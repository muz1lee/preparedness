#!/usr/bin/env python3
"""
本地打分脚本（无需 Docker）。

用法示例：

uv run python paperbench/scripts/grade_local_no_docker.py \
  --submissions-dir /abs/path/to/submissions \
  --out-dir ./grader_outputs \
  --code-only

目录结构要求（与 PBDirectSubmissionSolver 保持一致）：
<submissions_dir>/
  <paper_id>/
    <submission_folder_A>/
    <submission_folder_B>/
    ...

每个 <submission_folder_*> 会被打包成 submission.tar.gz（顶层名为 submission），
随后调用 grade_submission 进行评分，输出 JSON 写入 --out-dir。
默认使用 Gemini 2.5 Pro（与当前仓库的适配一致）。
"""

from __future__ import annotations

import argparse
import asyncio
import tarfile
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
import json
import re
from statistics import mean

import structlog

from paperbench.grade import grade_submission
from paperbench.paper_registry import paper_registry
from preparedness_turn_completer.google_completions_turn_completer import (
    GoogleCompletionsTurnCompleter,
)
from dotenv import load_dotenv


logger = structlog.stdlib.get_logger(component=__name__)


def _make_submission_tar(submission_dir: Path) -> str:
    """将 submission 目录打成 .tar.gz，并保证顶层目录名为 'submission'。返回临时文件路径。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    with tarfile.open(tmp.name, mode="w:gz") as tar:
        tar.add(str(submission_dir), arcname="submission")
    return tmp.name


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _append_progress(progress_dir: Path, paper_id: str, status: str, **extra: str) -> None:
    progress_dir.mkdir(parents=True, exist_ok=True)
    line = {"ts": _now_iso(), "paper_id": paper_id, "status": status}
    if extra:
        line.update(extra)
    with open(progress_dir / "progress.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


async def _grade_one(
    paper_id: str,
    submission_dir: Path,
    out_dir: Path,
    model: str,
    code_only: bool,
    resources_provided: bool,
    rep_idx: int = 0,
) -> tuple[bool, Path]:
    """对单个 submission 执行一次评分。

    目录结构调整为嵌套：
    <out_dir>/<paper_id>/<submission_name>/rep{N}_<UTC-micro>
    这样避免并发同秒导致的目录名冲突，并使 run 与 submission 的对应关系清晰。
    返回 (ok, paper_run_dir)。
    """
    # 高精度 UTC 时间戳（含微秒，后缀 Z）用于目录避免冲突
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
    submission_name = submission_dir.name
    # 每次评分的独立日志目录
    paper_run_dir = out_dir / paper_id / submission_name / f"rep{rep_idx + 1}_{ts}"
    paper_run_dir.mkdir(parents=True, exist_ok=True)
    out_json = paper_run_dir / "grader_output.json"

    cfg = GoogleCompletionsTurnCompleter.Config(model=model)

    _append_progress(paper_run_dir, paper_id, "packing_submission", submission=str(submission_dir))
    tar_path = _make_submission_tar(submission_dir)
    try:
        _append_progress(paper_run_dir, paper_id, "judging_start", archive=tar_path)
        try:
            # 传入 out_dir，让 SimpleJudge 能写叶子级进度文件 progress.json / progress.leaves.jsonl
            result = await grade_submission(
                submission_path=tar_path,
                grader_upload_path=str(out_json),
                paper_id=paper_id,
                judge_type="simple",
                completer_config=cfg,
                logger=logger,
                code_only=code_only,
                resources_provided=resources_provided,
                computer=None,
                out_dir=paper_run_dir,
            )
        except TypeError:
            # 兼容旧签名：无 out_dir 参数时退化为原调用
            result = await grade_submission(
                submission_path=tar_path,
                grader_upload_path=str(out_json),
                paper_id=paper_id,
                judge_type="simple",
                completer_config=cfg,
                logger=logger,
                code_only=code_only,
                resources_provided=resources_provided,
                computer=None,
            )
        ok = result is not None
        logger.info(
            "graded",
            paper_id=paper_id,
            submission=str(submission_dir),
            output=str(out_json),
            success=ok,
        )
        _append_progress(paper_run_dir, paper_id, "judging_done", success=str(ok))
        return ok, paper_run_dir
    except Exception as e:  # noqa: BLE001
        logger.exception("judging failed", paper_id=paper_id)
        _append_progress(paper_run_dir, paper_id, "judging_error", error=str(e))
        return False, paper_run_dir
    finally:
        # 临时 tar 包自动清理由系统完成；若需立即删除可在此手动删除
        _append_progress(paper_run_dir, paper_id, "cleanup")


def _ts_to_iso_z(ts: str) -> str:
    """将 grade.py 的 get_timestamp 风格（YYYY-MM-DDTHH-MM-SS-UTC）转换为 ISO Z（YYYY-MM-DDTHH:MM:SSZ）。若不匹配则原样返回。"""
    m = re.match(r"(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})", ts or "")
    if m:
        return f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}Z"
    return ts


def _build_comparisons(out_dir: Path, run_dirs: list[Path]) -> list[Path]:
    """基于本次运行成功的 run 目录，生成按 paper 聚合的对比 JSON。

    输出路径：<out_dir>/<paper_id>__comparison__<UTC>.json
    JSON 结构：
    {
      "paper_id": "...",
      "submissions": {
        "submission_v1": {
          "runs": [{"run_dir": "<relative>", "timestamp": "...Z", "score": 0.331, "coverage_pct": 1.0}, ...],
          "avg_score": 0.3365,
          "score_pct": 33.65
        },
        ...
      }
    }
    """
    per_paper: dict[str, dict[str, list[dict]]]= {}

    for rd in run_dirs:
        try:
            rel = rd.relative_to(out_dir)
            parts = rel.parts
            # 期望结构：<paper_id>/<submission_name>/<run_leaf>
            if len(parts) < 3:
                continue
            paper_id = parts[0]
            submission_name = parts[1]
            grader_json = rd / "grader_output.json"
            if not grader_json.exists():
                continue
            with open(grader_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            score = float(data.get("score", 0.0))
            cov = None
            if isinstance(data.get("progress"), dict):
                cov = data["progress"].get("coverage_pct")
            ts_raw = None
            if isinstance(data.get("time_cost"), dict):
                ts_raw = data["time_cost"].get("start_time")
            timestamp = _ts_to_iso_z(ts_raw) if ts_raw else data.get("graded_at")
            run_rec = {
                "run_dir": str(rel),  # 使用 out_dir 相对路径，便于定位
                "timestamp": timestamp,
                "score": round(score, 3),
                "coverage_pct": cov,
            }
            per_paper.setdefault(paper_id, {})
            per_paper[paper_id].setdefault(submission_name, [])
            per_paper[paper_id][submission_name].append(run_rec)
        except Exception:
            # 任意异常跳过该 run，保持聚合健壮性
            continue

    written: list[Path] = []
    # 为每个 paper 写出一个对比文件
    for paper_id, sub_map in per_paper.items():
        out_payload = {"paper_id": paper_id, "submissions": {}}
        for submission_name, runs in sorted(sub_map.items(), key=lambda kv: kv[0]):
            scores = [r.get("score", 0.0) for r in runs if isinstance(r.get("score"), (int, float))]
            avg = mean(scores) if scores else 0.0
            out_payload["submissions"][submission_name] = {
                "runs": runs,
                "avg_score": round(avg, 4),
                "score_pct": round(avg * 100.0, 2),
            }
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%fZ")
        comp_path = out_dir / f"{paper_id}__comparison__{ts}.json"
        with open(comp_path, "w", encoding="utf-8") as f:
            json.dump(out_payload, f, ensure_ascii=False, indent=2)
        written.append(comp_path)
    return written


async def _run(args: argparse.Namespace) -> int:
    submissions_root = Path(args.submissions_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    print('out_dir',out_dir)
    assert submissions_root.exists() and submissions_root.is_dir(), f"无效目录: {submissions_root}"

    valid_ids = set(paper_registry.list_paper_ids())
    paper_dirs = [p for p in submissions_root.iterdir() if p.is_dir()]

    tasks: list[asyncio.Task] = []
    sem = asyncio.Semaphore(args.max_concurrency)

    async def _guarded_grade(paper_id: str, subdir: Path, rep_idx: int) -> tuple[bool, Path]:
        async with sem:
            # 简单节流：在高并发或严格限额下建议使用 --sleep-between
            if args.sleep_between > 0:
                await asyncio.sleep(args.sleep_between)
            return await _grade_one(
                paper_id=paper_id,
                submission_dir=subdir,
                out_dir=out_dir,
                model=args.model,
                code_only=args.code_only,
                resources_provided=args.resources_provided,
                rep_idx=rep_idx,
            )

    for paper_dir in paper_dirs:
        paper_id = paper_dir.name
        if paper_id not in valid_ids:
            logger.warning("跳过无效 paper_id", paper_id=paper_id, path=str(paper_dir))
            continue
        subdirs = [p for p in paper_dir.iterdir() if p.is_dir()]
        for subdir in subdirs:
            # 对每个 submission 连续执行 N 次评分
            for rep_idx in range(max(1, args.repeat)):
                tasks.append(asyncio.create_task(_guarded_grade(paper_id, subdir, rep_idx)))

    if not tasks:
        logger.warning("未发现待评分的提交", submissions_dir=str(submissions_root))
        return 0

    results = await asyncio.gather(*tasks, return_exceptions=True)
    success_dirs: list[Path] = []
    ok = 0
    fail = 0
    for r in results:
        if isinstance(r, Exception):
            fail += 1
            continue
        ok_flag, run_dir = r
        if ok_flag:
            ok += 1
            success_dirs.append(run_dir)
        else:
            fail += 1
    logger.info("grading finished", success=ok, failed=fail, total=len(results))

    # 基于本次成功的 run 生成对比 JSON（按 paper 聚合）
    written = _build_comparisons(out_dir, success_dirs)
    if written:
        logger.info("comparison json written", files=[str(p) for p in written])
    return 0 if fail == 0 else 1


def main() -> None:
    # 加载本地 .env（若存在），便于无需 export 环境变量也能读取 API Key
    load_dotenv()
    parser = argparse.ArgumentParser(description="PaperBench 本地打分（Gemini，免 Docker）")
    parser.add_argument(
        "--submissions-dir",
        required=True,
        help="提交根目录：<dir>/<paper_id>/<submission_folder>",
    )
    parser.add_argument(
        "--out-dir",
        default="./grader_outputs",
        help="评分输出目录（默认 ./grader_outputs）",
    )
    parser.add_argument(
        "--model",
        default="gemini-2.5-pro",
        help="Gemini 模型名（默认 gemini-2.5-pro）",
    )
    parser.add_argument(
        "--code-only",
        action="store_true",
        help="仅代码评分模式（不依赖 reproduce.sh/log）（默认 False）",
    )
    parser.add_argument(
        "--resources-provided",
        action="store_true",
        help="标记额外资源已提供（默认 False）",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=4,
        help="并发评分上限（默认 4）",
    )
    parser.add_argument(
        "--sleep-between",
        type=float,
        default=0.0,
        help="每次调用之间的固定休眠秒数，用于限速（默认 0）",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="对每个 submission 重复评分次数（默认 1）",
    )

    args = parser.parse_args()

    # 预检查：确保有 Gemini API Key（GOOGLE_API_KEY 或 GEMINI_API_KEY）
    import os
    if not (os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        print("[FATAL] 缺少 Gemini API Key：请设置环境变量 GOOGLE_API_KEY 或 GEMINI_API_KEY，或在本目录 .env 中配置。")
        raise SystemExit(2)

    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
