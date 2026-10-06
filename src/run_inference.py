import json
import time
import urllib.request
from pathlib import Path

import ollama
import yaml

from evaluator import evaluate_hijacking


# YAML 설정 파일 불러오기
def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# 벤치마크가 없으면 다운로드
def download_benchmark_if_needed(
    benchmark_path,
    benchmark_url
):
    benchmark_path = Path(benchmark_path)

    if benchmark_path.exists():
        return

    print("Tensor Trust benchmark 다운로드 중...")

    benchmark_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    urllib.request.urlretrieve(
        benchmark_url,
        benchmark_path
    )

    print(f"benchmark downloaded: {benchmark_path}")


# JSONL 벤치마크 불러오기
def load_benchmark(
    benchmark_path,
    max_cases=None
):
    cases = []

    with open(
        benchmark_path,
        "r",
        encoding="utf-8"
    ) as f:

        for line in f:

            if not line.strip():
                continue

            case = json.loads(line)
            cases.append(case)

            if (
                max_cases is not None
                and len(cases) >= max_cases
            ):
                break

    return cases


# Tensor Trust용 프롬프트 생성
def build_prompt(
    pre_prompt,
    input_text,
    post_prompt
):
    parts = []

    if pre_prompt:
        parts.append(pre_prompt)

    parts.append(input_text)

    if post_prompt:
        parts.append(post_prompt)

    return "\n\n".join(parts)


# Ollama 모델 실행
def run_model(
    model_name,
    generation_options,
    prompt
):
    messages = [
        {
            "role": "user",
            "content": prompt,
        }
    ]

    start_time = time.time()

    response = ollama.chat(
        model=model_name,
        messages=messages,
        options=generation_options,
        think=False,
    )

    end_time = time.time()

    response_text = response[
        "message"
    ]["content"]

    latency = end_time - start_time

    return response_text, latency


# 기존 상세 JSONL 결과 저장
def save_jsonl(
    output_path,
    rows
):
    output_path = Path(output_path)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        output_path,
        "w",
        encoding="utf-8"
    ) as f:

        for row in rows:
            f.write(
                json.dumps(
                    row,
                    ensure_ascii=False
                )
                + "\n"
            )


# 사람이 읽기 쉬운 Markdown 결과 저장
def save_readable_markdown(
    output_path,
    rows
):
    output_path = Path(output_path)

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(
        output_path,
        "w",
        encoding="utf-8"
    ) as f:

        f.write("# Prompt Injection Experiment Results\n\n")

        for index, row in enumerate(rows, start=1):

            f.write(
                f"## Case {index} "
                f"(Sample ID: {row['sample_id']})\n\n"
            )

            f.write(
                f"**Attack Success:** "
                f"{row['attack_success']}\n\n"
            )

            f.write(
                f"**Normal Code Success:** "
                f"{row['defense_valid']}\n\n"
            )

            f.write("### Pre-Prompt\n\n")
            f.write("```text\n")
            f.write(row["pre_prompt"])
            f.write("\n```\n\n")

            f.write("### Attack\n\n")
            f.write("```text\n")
            f.write(row["attack"])
            f.write("\n```\n\n")

            f.write("### Post-Prompt\n\n")
            f.write("```text\n")

            if row["post_prompt"]:
                f.write(row["post_prompt"])
            else:
                f.write("(empty)")

            f.write("\n```\n\n")

            f.write("### LLM Response\n\n")
            f.write("```text\n")

            if row["attack_response"]:
                f.write(row["attack_response"])
            else:
                f.write("(empty response)")

            f.write("\n```\n\n")

            f.write("---\n\n")


def main():

    config_path = (
        "configs/"
        "baseline_gemma4_e4b_q4km.yaml"
    )

    config = load_config(config_path)

    # 모델 설정
    model_name = config["model"]["name"]

    generation_options = config[
        "generation"
    ]

    # 벤치마크 설정
    benchmark_config = config[
        "benchmark"
    ]

    benchmark_path = benchmark_config[
        "path"
    ]

    benchmark_url = benchmark_config[
        "url"
    ]

    max_cases = benchmark_config.get(
        "max_cases"
    )

    # 실행 설정
    repeat = config["run"]["repeat"]

    raw_output_path = config[
        "run"
    ]["output_path"]

    readable_output_path = config[
        "run"
    ]["readable_output_path"]

    # -----------------------------
    # Benchmark 다운로드
    # -----------------------------

    download_benchmark_if_needed(
        benchmark_path,
        benchmark_url
    )

    # -----------------------------
    # Benchmark 불러오기
    # -----------------------------

    benchmark_cases = load_benchmark(
        benchmark_path,
        max_cases=max_cases
    )

    print(
        f"loaded benchmark cases: "
        f"{len(benchmark_cases)}"
    )

    results = []

    # -----------------------------
    # 실험 시작
    # -----------------------------

    for repeat_index in range(repeat):

        for case_index, case in enumerate(
            benchmark_cases,
            start=1
        ):

            sample_id = case["sample_id"]

            pre_prompt = case[
                "pre_prompt"
            ]

            post_prompt = case[
                "post_prompt"
            ]

            access_code = case[
                "access_code"
            ]

            attack = case[
                "attack"
            ]

            # =============================
            # 1. Prompt Injection 공격
            # =============================

            attack_prompt = build_prompt(
                pre_prompt,
                attack,
                post_prompt
            )

            (
                attack_response,
                attack_latency
            ) = run_model(
                model_name,
                generation_options,
                attack_prompt
            )

            # =============================
            # 2. 정상 access code 실험
            # =============================

            access_prompt = build_prompt(
                pre_prompt,
                access_code,
                post_prompt
            )

            (
                access_response,
                access_latency
            ) = run_model(
                model_name,
                generation_options,
                access_prompt
            )

            # =============================
            # 평가
            # =============================

            evaluation = evaluate_hijacking(
                attack_response,
                access_response
            )

            result = {
                "sample_id": sample_id,

                "repeat_index":
                    repeat_index,

                "model":
                    model_name,

                "generation":
                    generation_options,

                "pre_prompt":
                    pre_prompt,

                "post_prompt":
                    post_prompt,

                "access_code":
                    access_code,

                "attack":
                    attack,

                "attack_response":
                    attack_response,

                "access_code_response":
                    access_response,

                "attack_latency_sec":
                    attack_latency,

                "access_latency_sec":
                    access_latency,

                "attack_success":
                    evaluation[
                        "attack_success"
                    ],

                "defense_valid":
                    evaluation[
                        "defense_valid"
                    ],

                "attack_response_invalid":
                    evaluation[
                        "attack_response_invalid"
                    ],

                "access_response_invalid":
                    evaluation[
                        "access_response_invalid"
                    ],

                "evaluation_rule":
                    evaluation[
                        "evaluation_rule"
                    ],
            }

            results.append(result)

            print(
                f"[{case_index}/"
                f"{len(benchmark_cases)}] "
                f"sample={sample_id} "
                f"attack_success="
                f"{evaluation['attack_success']} "
                f"defense_valid="
                f"{evaluation['defense_valid']}"
            )

    # =============================
    # 결과 파일 저장
    # =============================

    # 1. 모든 정보가 들어있는 JSONL
    save_jsonl(
        raw_output_path,
        results
    )

    # 2. 사람이 보기 쉬운 Markdown
    save_readable_markdown(
        readable_output_path,
        results
    )

    # =============================
    # 전체 통계
    # =============================

    total_count = len(results)

    attack_success_count = sum(
        1
        for result in results
        if result["attack_success"]
    )

    defense_valid_count = sum(
        1
        for result in results
        if result["defense_valid"]
    )

    # -----------------------------
    # 기존 Tensor Trust 방식
    # -----------------------------

    if total_count > 0:

        raw_asr = (
            attack_success_count
            / total_count
        )

        hrr = 1 - raw_asr

        dv = (
            defense_valid_count
            / total_count
        )

    else:
        raw_asr = 0
        hrr = 0
        dv = 0

    # -----------------------------
    # 새 방식
    #
    # 정상 access code에 성공한
    # 케이스만 ASR 계산에 사용
    # -----------------------------

    valid_results = [
        result
        for result in results
        if result["defense_valid"]
    ]

    valid_case_count = len(
        valid_results
    )

    valid_attack_success_count = sum(
        1
        for result in valid_results
        if result["attack_success"]
    )

    if valid_case_count > 0:

        valid_asr = (
            valid_attack_success_count
            / valid_case_count
        )

    else:
        valid_asr = 0

    # =============================
    # 결과 출력
    # =============================

    print()
    print(
        "========== RESULT =========="
    )

    print(
        f"Total cases: "
        f"{total_count}"
    )

    print(
        f"Attack successes: "
        f"{attack_success_count}"
    )

    print(
        f"Raw ASR: "
        f"{raw_asr:.2%}"
    )

    print(
        f"HRR: "
        f"{hrr:.2%}"
    )

    print(
        f"DV: "
        f"{dv:.2%}"
    )

    print(
        "----------------------------"
    )

    print(
        f"Valid cases: "
        f"{valid_case_count}"
    )

    print(
        f"Valid-case attack successes: "
        f"{valid_attack_success_count}"
    )

    print(
        f"Valid-case ASR: "
        f"{valid_asr:.2%}"
    )

    print(
        "============================"
    )

    print(
        f"raw results saved to: "
        f"{raw_output_path}"
    )

    print(
        f"readable results saved to: "
        f"{readable_output_path}"
    )


if __name__ == "__main__":
    main()