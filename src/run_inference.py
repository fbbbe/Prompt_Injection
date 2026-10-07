"""Run Tensor Trust prompt-hijacking benchmarks with Ollama.

Run from any working directory:
    python src/run_inference.py

This file belongs at: <project root>/src/run_inference.py
"""

import hashlib
import json
import os
import shutil
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import httpx
import ollama
import yaml

from evaluator import evaluate_hijacking


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "configs" / "baseline_gemma4_e4b_q4km.yaml"
CLIENT = ollama.Client(timeout=None)  # Allow slow CPU inference without a read timeout.


def project_path(value):
    path = Path(value)
    return path if path.is_absolute() else ROOT_DIR / path


def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def download_benchmark_if_needed(benchmark_path, benchmark_url):
    if benchmark_path.exists():
        return
    if not benchmark_url:
        raise FileNotFoundError(f"Benchmark missing and no URL configured: {benchmark_path}")
    benchmark_path.parent.mkdir(parents=True, exist_ok=True)
    print("Tensor Trust benchmark 다운로드 중...")
    urllib.request.urlretrieve(benchmark_url, benchmark_path)
    print(f"benchmark downloaded: {benchmark_path}")


def load_benchmark(benchmark_path, max_cases=None):
    cases = []
    with open(benchmark_path, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            if not line.strip():
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSONL at line {line_number}: {e}") from e
            if max_cases is not None and len(cases) >= int(max_cases):
                break
    return cases


def build_prompt(pre_prompt, input_text, post_prompt):
    parts = []
    if pre_prompt:
        parts.append(str(pre_prompt))
    parts.append(str(input_text))
    if post_prompt:
        parts.append(str(post_prompt))
    return "\n\n".join(parts)


def run_model(model_name, generation_options, prompt, max_retries=1):
    """Always return a result dict. Do not confuse errors with empty model answers."""
    messages = [{"role": "user", "content": prompt}]
    start_time = time.perf_counter()

    for attempt in range(max_retries + 1):
        try:
            response = CLIENT.chat(
                model=model_name,
                messages=messages,
                options=generation_options,
                think=False,
            )
            response_text = response.message.content or ""
            return {
                "response": response_text,
                "latency_sec": time.perf_counter() - start_time,
                "status": "ok" if response_text.strip() else "empty_response",
                "error": None,
            }
        except (httpx.TransportError, httpx.TimeoutException) as e:
            # Retry only transient transport errors, not model-generated HTTP 500s.
            if attempt < max_retries:
                print(f"  HTTP 연결 오류, 재시도 {attempt + 1}/{max_retries}: {e}")
                time.sleep(min(2 ** attempt, 5))
                continue
            error = e
            break
        except Exception as e:
            # Includes ollama.ResponseError (e.g., token repeat limit reached).
            error = e
            break

    error_message = f"{type(error).__name__}: {error}"
    print(f"  Ollama 생성 오류: {error_message}")
    return {
        "response": "",
        "latency_sec": time.perf_counter() - start_time,
        "status": "generation_error",
        "error": error_message,
    }


def atomic_write(path, text):
    path = project_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    with open(temp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp_path, path)


def save_jsonl(output_path, rows):
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    atomic_write(output_path, content)


def markdown_fence(value):
    """Use a long enough code fence even when a prompt contains backticks."""
    text = "" if value is None else str(value)
    longest_run = max((len(s) for s in __import__("re").findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest_run + 1)
    return f"{fence}text\n{text if text else '(empty)'}\n{fence}\n\n"


def save_readable_markdown(output_path, rows):
    parts = ["# Prompt Injection Experiment Results\n\n"]
    for index, row in enumerate(rows, start=1):
        parts.extend([
            f"## Case {index} (Sample ID: {row['sample_id']})\n\n",
            f"**Repeat:** {row['repeat_index'] + 1}\n\n",
            f"**Attack Success:** {row['attack_success']}\n\n",
            f"**Normal Code Success:** {row['defense_valid']}\n\n",
            f"**Attack Status:** {row['attack_status']}\n\n",
            f"**Normal Code Status:** {row['access_status']}\n\n",
        ])
        if row.get("attack_error"):
            parts.append(f"**Attack Error:** {row['attack_error']}\n\n")
        if row.get("access_error"):
            parts.append(f"**Normal Code Error:** {row['access_error']}\n\n")
        for heading, key in (
            ("Pre-Prompt", "pre_prompt"),
            ("Attack", "attack"),
            ("Post-Prompt", "post_prompt"),
            ("LLM Response", "attack_response"),
        ):
            parts.append(f"### {heading}\n\n")
            parts.append(markdown_fence(row.get(key)))
        parts.append("---\n\n")
    atomic_write(output_path, "".join(parts))


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def make_signature(model_name, generation_options, benchmark_path, repeat):
    evaluator_path = Path(__file__).with_name("evaluator.py")
    signature_data = {
        "schema": "tensor-trust-robust-v1",
        "model": model_name,
        "generation": generation_options,
        "benchmark_sha256": file_sha256(benchmark_path),
        "evaluator_sha256": file_sha256(evaluator_path) if evaluator_path.exists() else None,
        "repeat": repeat,
    }
    encoded = json.dumps(signature_data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def archive_file_if_exists(path):
    path = project_path(path)
    if not path.exists():
        return
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = path.with_name(path.name + f".backup_{timestamp}")
    suffix = 2
    while archive.exists():
        archive = path.with_name(path.name + f".backup_{timestamp}_{suffix}")
        suffix += 1
    shutil.move(str(path), str(archive))
    print(f"기존 결과 백업: {archive}")


def load_previous_results(raw_output_path, readable_output_path, signature, expected_cases):
    raw_output_path = project_path(raw_output_path)
    if not raw_output_path.exists():
        return []
    rows = []
    with open(raw_output_path, "r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    raise ValueError(f"Existing results JSONL corrupted at line {line_number}") from e

    matching = all(
        row.get("run_signature") == signature
        and isinstance(row.get("case_index"), int)
        and isinstance(row.get("repeat_index"), int)
        and 1 <= row["case_index"] <= expected_cases
        for row in rows
    )
    if not matching:
        print("기존 결과와 현재 실험 설정이 달라 별도 백업 후 새로 시작합니다.")
        archive_file_if_exists(raw_output_path)
        archive_file_if_exists(readable_output_path)
        return []

    # Last occurrence wins if the file contains duplicates.
    unique = {(row["repeat_index"], row["case_index"]): row for row in rows}
    print(f"이전 실행 결과 {len(unique)}개 복구 완료")
    return [unique[key] for key in sorted(unique)]


def rate_string(numerator, denominator):
    return f"{numerator / denominator:.2%}" if denominator else "N/A"


def print_summary(results, raw_output_path, readable_output_path):
    evaluated = [
        r for r in results
        if isinstance(r.get("attack_success"), bool)
        and isinstance(r.get("defense_valid"), bool)
    ]
    valid_results = [r for r in evaluated if r["defense_valid"]]
    attack_success_count = sum(r["attack_success"] for r in evaluated)
    defense_valid_count = len(valid_results)
    valid_attack_success_count = sum(r["attack_success"] for r in valid_results)
    error_count = len(results) - len(evaluated)
    empty_response_count = sum(
        r.get("attack_status") == "empty_response" or r.get("access_status") == "empty_response"
        for r in results
    )

    print("\n========== RESULT ==========")
    print(f"Total processed cases: {len(results)}")
    print(f"Evaluated cases (generation errors excluded): {len(evaluated)}")
    print(f"Generation error cases: {error_count}")
    print(f"Empty response cases: {empty_response_count}")
    print(f"Attack successes: {attack_success_count}")
    print(f"Raw ASR: {rate_string(attack_success_count, len(evaluated))}")
    print(f"HRR: {rate_string(len(evaluated) - attack_success_count, len(evaluated))}")
    print(f"DV: {rate_string(defense_valid_count, len(evaluated))}")
    print("----------------------------")
    print(f"Valid cases: {len(valid_results)}")
    print(f"Valid-case attack successes: {valid_attack_success_count}")
    print(f"Valid-case ASR: {rate_string(valid_attack_success_count, len(valid_results))}")
    print("============================")
    print(f"raw results saved to: {raw_output_path}")
    print(f"readable results saved to: {readable_output_path}")


def main():
    config = load_config(CONFIG_PATH)
    model_name = config["model"]["name"]
    generation_options = config["generation"]
    benchmark_config = config["benchmark"]
    benchmark_path = project_path(benchmark_config["path"])
    benchmark_url = benchmark_config.get("url")
    max_cases = benchmark_config.get("max_cases")

    run_config = config["run"]
    repeat = int(run_config.get("repeat", 1))
    max_retries = int(run_config.get("max_retries", 1))
    raw_output_path = project_path(run_config["output_path"])
    readable_output_path = project_path(run_config["readable_output_path"])

    download_benchmark_if_needed(benchmark_path, benchmark_url)
    benchmark_cases = load_benchmark(benchmark_path, max_cases)
    print(f"loaded benchmark cases: {len(benchmark_cases)}")

    signature = make_signature(model_name, generation_options, benchmark_path, repeat)
    results = load_previous_results(
        raw_output_path, readable_output_path, signature, len(benchmark_cases)
    )
    completed = {(r["repeat_index"], r["case_index"]) for r in results}

    try:
        for repeat_index in range(repeat):
            for case_index, case in enumerate(benchmark_cases, start=1):
                case_key = (repeat_index, case_index)
                if case_key in completed:
                    continue

                sample_id = case["sample_id"]
                pre_prompt = case["pre_prompt"]
                post_prompt = case["post_prompt"]
                access_code = case["access_code"]
                attack = case["attack"]

                attack_prompt = build_prompt(pre_prompt, attack, post_prompt)
                attack_result = run_model(
                    model_name, generation_options, attack_prompt, max_retries
                )

                access_prompt = build_prompt(pre_prompt, access_code, post_prompt)
                access_result = run_model(
                    model_name, generation_options, access_prompt, max_retries
                )

                if (
                    attack_result["status"] == "generation_error"
                    or access_result["status"] == "generation_error"
                ):
                    # Never report infrastructure errors as attack success/failure.
                    evaluation = {
                        "attack_success": None,
                        "defense_valid": None,
                        "attack_response_invalid": None,
                        "access_response_invalid": None,
                        "evaluation_rule": "generation_error_not_evaluated",
                    }
                else:
                    evaluation = evaluate_hijacking(
                        attack_result["response"], access_result["response"]
                    )

                result = {
                    "sample_id": sample_id,
                    "repeat_index": repeat_index,
                    "case_index": case_index,
                    "run_signature": signature,
                    "model": model_name,
                    "generation": generation_options,
                    "pre_prompt": pre_prompt,
                    "post_prompt": post_prompt,
                    "access_code": access_code,
                    "attack": attack,
                    "attack_response": attack_result["response"],
                    "access_code_response": access_result["response"],
                    "attack_latency_sec": attack_result["latency_sec"],
                    "access_latency_sec": access_result["latency_sec"],
                    "attack_status": attack_result["status"],
                    "access_status": access_result["status"],
                    "attack_error": attack_result["error"],
                    "access_error": access_result["error"],
                    "attack_success": evaluation["attack_success"],
                    "defense_valid": evaluation["defense_valid"],
                    "attack_response_invalid": evaluation["attack_response_invalid"],
                    "access_response_invalid": evaluation["access_response_invalid"],
                    "evaluation_rule": evaluation["evaluation_rule"],
                }

                results.append(result)
                completed.add(case_key)
                save_jsonl(raw_output_path, results)  # Checkpoint after every case.
                save_readable_markdown(readable_output_path, results)

                print(
                    f"[{case_index}/{len(benchmark_cases)}] "
                    f"repeat={repeat_index + 1} sample={sample_id} "
                    f"attack_status={result['attack_status']} "
                    f"access_status={result['access_status']} "
                    f"attack_success={result['attack_success']} "
                    f"defense_valid={result['defense_valid']}"
                )
    except KeyboardInterrupt:
        print("\n사용자가 중단했습니다. 마지막으로 완료한 케이스까지 저장되어 있습니다.")
    finally:
        # Rebuild readable output when resuming a previously interrupted run.
        if results:
            save_readable_markdown(readable_output_path, results)
        print_summary(results, raw_output_path, readable_output_path)


if __name__ == "__main__":
    main()
