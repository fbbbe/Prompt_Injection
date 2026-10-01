import json
import time
from pathlib import Path

import ollama
import yaml

from evaluator import evaluate_response


# YAML 설정 파일 불러옴
def load_config(config_path):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) #yaml.safe_load() 함수를 사용하여 YAML 파일을 파이썬 객체로 변환


# JSONL 형식의 벤치마크 데이터 불러옴
def load_benchmark(benchmark_path):
    cases = []

    with open(benchmark_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                cases.append(json.loads(line)) #json.loads() 함수를 사용하여 각 줄을 파이썬 객체로 변환하고 cases 리스트에 추가

    return cases

# Ollama 모델 실행하고 응답과 실행 시간 반환
def run_model(model_name, generation_options, system_prompt, user_prompt):
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    start_time = time.time()

    response = ollama.chat(
        model=model_name,
        messages=messages,
        options=generation_options,
        think=False,
    )

    # 전체 응답 확인용 출력
    print("\n===== FULL OLLAMA RESPONSE =====")
    print(response)
    print("================================\n")

    end_time = time.time()

    response_text = response["message"]["content"]
    latency = end_time - start_time

    return response_text, latency


# 실험 결과를 JSONL 파일로 저장
def save_jsonl(output_path, rows):
    output_path = Path(output_path)

    # 저장할 폴더 없으면 생성
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main():
    # 실험 설정 파일 경로
    config_path = "configs/baseline_gemma4_e4b_q4km.yaml"

    config = load_config(config_path)

    # 설정값 가져옴
    model_name = config["model"]["name"]
    generation_options = config["generation"]
    system_prompt = config["prompt"]["system_prompt"]
    benchmark_path = config["benchmark"]["path"]
    output_path = config["run"]["output_path"]
    repeat = config["run"]["repeat"]

    # 공격 벤치마크 불러옴
    benchmark_cases = load_benchmark(benchmark_path)

    results = []

    # 설정한 횟수만큼 실험 반복
    for repeat_index in range(repeat):
        for case in benchmark_cases:

            # 모델 실행
            response_text, latency = run_model(
                model_name=model_name,
                generation_options=generation_options,
                system_prompt=system_prompt,
                user_prompt=case["user_prompt"],
            )

            # 모델 응답의 공격 성공 여부 평가
            evaluation = evaluate_response(case, response_text)

            # 현재 실험 결과 저장
            result = {
                "case_id": case["id"],
                "repeat_index": repeat_index,
                "attack_type": case["attack_type"],
                "attack_family": case.get("attack_family"),
                "model": model_name,
                "generation": generation_options,
                "system_prompt": system_prompt,
                "user_prompt": case["user_prompt"],
                "response": response_text,
                "latency_sec": latency,
                "attack_success": evaluation["attack_success"],
                "matched_forbidden_strings": evaluation["matched_forbidden_strings"],
                "invalid_response": evaluation["invalid_response"],
                "evaluation_rule": evaluation["evaluation_rule"],
            }

            results.append(result)

            # 진행 상황 출력
            print(f"finished: {case['id']} repeat={repeat_index}")

    # 전체 결과 저장
    save_jsonl(output_path, results)

    # 공격 성공 횟수와 ASR 계산
    attack_success_count = sum(
        1 for r in results if r["attack_success"]
    )

    total_count = len(results)

    asr = (
        attack_success_count / total_count
        if total_count > 0
        else 0
    )

    # 최종 결과 출력
    print(f"attack_success_count: {attack_success_count}")
    print(f"total_count: {total_count}")
    print(f"ASR: {asr:.2%}")
    print(f"saved results to {output_path}")


if __name__ == "__main__":
    main()