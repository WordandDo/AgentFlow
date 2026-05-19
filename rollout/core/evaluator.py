"""
Evaluator - evaluates agent predictions against ground truth

Supports multiple metrics: exact_match, f1_score, contains_answer, etc.

The evaluator deliberately uses the *synchronous* OpenAI client
(`create_openai_client` + `chat_completion`). Rollout itself runs on
`AsyncOpenAI` (see Phase 1, commit 1.1) but the evaluator is short,
serial, and called from sync user scripts; keeping it on the sync
client avoids forcing every downstream caller into an event loop just
to compute a score. Phase 5 (commit 5.2) will add an opt-in async
variant for large-scale judge runs.
"""

import re
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional, Callable
from collections import Counter

from .models import TaskResult, EvaluationResult
from .utils import normalize_answer, create_openai_client, chat_completion


class Evaluator:
    """
    Evaluates agent predictions using various metrics.
    
    Supported metrics:
    - exact_match: Exact string match (after normalization)
    - f1_score: Token-level F1 score
    - contains_answer: Check if prediction contains ground truth
    - numeric_match: Compare numeric values
    - similarity: Semantic similarity (requires embedding model)
    - llm_judgement: Use LLM to judge correctness
    - DocBench_LasJ: DocBench official evaluation method (requires question and optional evidence)
    - MMLongBench-Doc_LasJ: MMLongBench-Doc official evaluation method (requires question)
    - MMLongBench-Doc_F1: MMLongBench-Doc official F1-score metric (requires question and answer_format)
    - MMLongBench-Doc_Acc: MMLongBench-Doc official Accuracy metric (requires question and answer_format)
    """

    def __init__(
        self,
        metric: str = "exact_match",
        model_name: str = "gpt-4.1-2025-04-14",
        api_key: str = "",
        base_url: str = "",
        temperature: float = 0.0,
        max_retries: int = 3,
        extra_params: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize evaluator.
        
        Args:
            metric: Evaluation metric to use
            model_name: Model for LLM-based evaluation
            api_key: API key from rollout config
            base_url: API base URL from rollout config
            temperature: Sampling temperature for llm_judgement
            max_retries: Retry attempts for llm_judgement
            extra_params: Extra completion params for llm_judgement
        """
        self.metric = metric
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.temperature = temperature
        self.max_retries = max_retries
        self.extra_params = extra_params or {}
        self._client = None  # Lazy initialization for LLM metrics

    def evaluate(self, results: List[TaskResult]) -> Dict[str, Any]:
        """
        Evaluate all results.
        
        Args:
            results: List of task results
            
        Returns:
            Evaluation summary with scores and details
        """
        evaluations = []
        scores = []
        
        for result in results:
            if not result.success:
                # Failed tasks get score 0 (and remember it on the TaskResult
                # so downstream code can sort / filter without joining the
                # separate evaluation file).
                eval_result = EvaluationResult(
                    task_id=result.task_id,
                    predicted=result.predicted_answer,
                    ground_truth=result.ground_truth or "",
                    score=0.0,
                    metric=self.metric,
                    details={"error": result.error}
                )
                result.score = 0.0
            elif result.ground_truth is None:
                # No ground truth available. Keep evaluation summary
                # consistent with previous behaviour (score=0.0 contributes
                # to "not evaluated"), but on the TaskResult itself leave
                # `score=None` so callers can distinguish "no GT" from a
                # genuine zero.
                eval_result = EvaluationResult(
                    task_id=result.task_id,
                    predicted=result.predicted_answer,
                    ground_truth="",
                    score=0.0,
                    metric=self.metric,
                    details={"note": "No ground truth available"}
                )
                result.score = None
            else:
                # Evaluate prediction
                # Get evidence from metadata if available (for DocBench)
                evidence = result.metadata.get("evidence", "") if result.metadata else ""
                # Get answer_format from metadata if available (for MMLongBench-Doc)
                answer_format = result.metadata.get("answer_format", "") if result.metadata else ""
                score, details = self._evaluate_single(
                    result.predicted_answer,
                    result.ground_truth,
                    question=result.question,
                    evidence=evidence,
                    answer_format=answer_format
                )
                eval_result = EvaluationResult(
                    task_id=result.task_id,
                    predicted=result.predicted_answer,
                    ground_truth=result.ground_truth,
                    score=score,
                    metric=self.metric,
                    details=details
                )
                result.score = score
                scores.append(score)
            
            evaluations.append(eval_result)
        
        # Calculate summary statistics
        # For MMLongBench-Doc_F1, calculate F1-score
        if self.metric == "MMLongBench-Doc_F1":
            avg_score = self._calculate_mmlongbench_f1(evaluations)
        # For MMLongBench-Doc_Acc, average_score is already Accuracy
        elif self.metric == "MMLongBench-Doc_Acc":
            avg_score = sum(scores) / len(scores) if scores else 0.0
        else:
            avg_score = sum(scores) / len(scores) if scores else 0.0
        
        perfect_matches = sum(1 for s in scores if s >= 0.99)
        
        return {
            "metric": self.metric,
            "total_tasks": len(results),
            "evaluated_tasks": len(scores),
            "average_score": avg_score,
            "perfect_matches": perfect_matches,
            "success_rate": len(scores) / len(results) if results else 0.0,
            "evaluations": [e.to_dict() for e in evaluations]
        }

    def _evaluate_single(self, predicted: str, ground_truth: str, question: str = "", evidence: str = "", answer_format: str = "") -> tuple:
        """
        Evaluate single prediction.
        
        Args:
            predicted: Predicted answer
            ground_truth: Ground truth answer
            question: Question text (required for some metrics like DocBench_LasJ)
            evidence: Reference text/evidence (optional, for DocBench_LasJ)
        
        Returns:
            (score, details_dict)
        """
        metric_fn = self._get_metric_fn()
        # Pass question and evidence to metric function if it supports them
        if self.metric == "DocBench_LasJ":
            return metric_fn(predicted, ground_truth, question=question, evidence=evidence)
        elif self.metric == "MMLongBench-Doc_LasJ":
            return metric_fn(predicted, ground_truth, question=question)
        elif self.metric in ["MMLongBench-Doc_F1", "MMLongBench-Doc_Acc"]:
            return metric_fn(predicted, ground_truth, question=question, answer_format=answer_format)
        else:
            return metric_fn(predicted, ground_truth)

    def _get_metric_fn(self) -> Callable:
        """Get the metric function based on config"""
        metrics = {
            "exact_match": self._exact_match,
            "f1_score": self._f1_score,
            "contains_answer": self._contains_answer,
            "numeric_match": self._numeric_match,
            "similarity": self._similarity,
            "llm_judgement": self._llm_judgement,
            "DocBench_LasJ": self._docbench_lasj,
            "MMLongBench-Doc_LasJ": self._mmlongbench_doc_lasj,
            "MMLongBench-Doc_F1": self._mmlongbench_doc_f1,
            "MMLongBench-Doc_Acc": self._mmlongbench_doc_acc,
        }
        
        if self.metric not in metrics:
            raise ValueError(f"Unknown metric: {self.metric}")
        
        return metrics[self.metric]

    def _exact_match(self, predicted: str, ground_truth: str) -> tuple:
        """Exact match after normalization"""
        pred_norm = normalize_answer(predicted)
        gt_norm = normalize_answer(ground_truth)
        
        match = pred_norm == gt_norm
        score = 1.0 if match else 0.0
        
        return score, {
            "normalized_predicted": pred_norm,
            "normalized_ground_truth": gt_norm,
            "match": match
        }

    def _f1_score(self, predicted: str, ground_truth: str) -> tuple:
        """Token-level F1 score"""
        pred_tokens = normalize_answer(predicted).split()
        gt_tokens = normalize_answer(ground_truth).split()
        
        if not pred_tokens or not gt_tokens:
            return 0.0, {"precision": 0.0, "recall": 0.0, "f1": 0.0}
        
        pred_counter = Counter(pred_tokens)
        gt_counter = Counter(gt_tokens)
        
        # Count common tokens
        common = sum((pred_counter & gt_counter).values())
        
        precision = common / len(pred_tokens) if pred_tokens else 0.0
        recall = common / len(gt_tokens) if gt_tokens else 0.0
        
        if precision + recall == 0:
            f1 = 0.0
        else:
            f1 = 2 * precision * recall / (precision + recall)
        
        return f1, {"precision": precision, "recall": recall, "f1": f1}

    def _contains_answer(self, predicted: str, ground_truth: str) -> tuple:
        """Check if prediction contains ground truth"""
        pred_norm = normalize_answer(predicted)
        gt_norm = normalize_answer(ground_truth)
        
        contains = gt_norm in pred_norm
        score = 1.0 if contains else 0.0
        
        return score, {"contains": contains}

    def _numeric_match(self, predicted: str, ground_truth: str) -> tuple:
        """Compare numeric values with tolerance"""
        def extract_numbers(text: str) -> List[float]:
            numbers = re.findall(r'-?\d+\.?\d*', text)
            return [float(n) for n in numbers]
        
        pred_nums = extract_numbers(predicted)
        gt_nums = extract_numbers(ground_truth)
        
        if not pred_nums or not gt_nums:
            return 0.0, {"pred_numbers": pred_nums, "gt_numbers": gt_nums}
        
        # Check if any predicted number matches any ground truth number
        tolerance = 1e-6
        for p in pred_nums:
            for g in gt_nums:
                if abs(p - g) <= tolerance or (g != 0 and abs((p - g) / g) <= 0.01):
                    return 1.0, {
                        "matched_pred": p,
                        "matched_gt": g,
                        "pred_numbers": pred_nums,
                        "gt_numbers": gt_nums
                    }
        
        return 0.0, {"pred_numbers": pred_nums, "gt_numbers": gt_nums}

    def _similarity(self, predicted: str, ground_truth: str) -> tuple:
        """Semantic similarity using embeddings"""
        # Simple character-level similarity as fallback
        # In production, use embedding-based similarity
        pred_set = set(normalize_answer(predicted).lower())
        gt_set = set(normalize_answer(ground_truth).lower())
        
        if not pred_set or not gt_set:
            return 0.0, {}
        
        intersection = len(pred_set & gt_set)
        union = len(pred_set | gt_set)
        jaccard = intersection / union if union > 0 else 0.0
        
        return jaccard, {"jaccard_similarity": jaccard}

    def _llm_judgement(self, predicted: str, ground_truth: str) -> tuple:
        """Use LLM to judge correctness"""
        if self._client is None:
            self._client = create_openai_client(api_key=self.api_key, base_url=self.base_url)
        
        prompt = f"""You are an expert evaluator. Judge if the predicted answer is correct based on the ground truth.

Ground Truth Answer: {ground_truth}
Predicted Answer: {predicted}

Consider the following:
1. The predicted answer may be phrased differently but still be correct
2. Minor differences in formatting or punctuation should not affect correctness
3. The core information must match

Respond with ONLY a JSON object:
{{"correct": true/false, "reasoning": "brief explanation"}}
"""
        
        try:
            response = chat_completion(
                self._client,
                max_retries=self.max_retries,
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.temperature,
                **self.extra_params,
            )
            
            content = response.choices[0].message.content
            
            # Parse response
            try:
                result = json.loads(content)
                correct = result.get("correct", False)
                reasoning = result.get("reasoning", "")
            except json.JSONDecodeError:
                # Try to extract from text
                correct = "true" in content.lower() and "false" not in content.lower()
                reasoning = content
            
            score = 1.0 if correct else 0.0
            return score, {"correct": correct, "reasoning": reasoning}
            
        except Exception as e:
            return 0.0, {"error": str(e)}

    def _docbench_lasj(self, predicted: str, ground_truth: str, question: str = "", evidence: str = "") -> tuple:
        """DocBench official evaluation method (LasJ)"""
        if self._client is None:
            self._client = create_openai_client(api_key=self.api_key, base_url=self.base_url)
        
        # DocBench evaluation prompt template
        eval_prompt = """Task Overview:
You are tasked with evaluating user answers based on a given question, reference answer, and additional reference text. Your goal is to assess the correctness of the user answer using a specific metric.

Evaluation Criteria:
1. Yes/No Questions: Verify if the user's answer aligns with the reference answer in terms of a "yes" or "no" response.
2. Short Answers/Directives: Ensure key details such as numbers, specific nouns/verbs, and dates match those in the reference answer.
3. Abstractive/Long Answers: The user's answer can differ in wording but must convey the same meaning and contain the same key information as the reference answer to be considered correct.

Evaluation Process:
1. Identify the type of question presented.
2. Apply the relevant criteria from the Evaluation Criteria.
3. Compare the user's answer against the reference answer accordingly.
4. Consult the reference text for clarification when needed.
5. Score the answer with a binary label 0 or 1, where 0 denotes wrong and 1 denotes correct.
NOTE that if the user answer is 0 or an empty string, it should get a 0 score.

Question: {question}
User Answer: {sys_ans}
Reference Answer: {ref_ans}
Reference Text: {ref_text}

Evaluation Form (score ONLY):
- Correctness: """.format(
            question=question,
            sys_ans=predicted,
            ref_ans=ground_truth,
            ref_text=evidence if evidence else ""
        )
        
        system_content = 'You are a helpful evaluator.'
        
        try:
            response = chat_completion(
                self._client,
                max_retries=self.max_retries,
                model=self.model_name,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": eval_prompt}
                ],
                temperature=self.temperature,
                **self.extra_params,
            )
            
            content = response.choices[0].message.content.strip()
            
            # Parse the score from response (should be 0 or 1)
            # Try to extract numeric value
            score = 0.0
            try:
                # Look for "0" or "1" in the response
                if "1" in content and "0" not in content.split("1")[0]:
                    # Check if 1 appears before any 0
                    if content.find("1") < content.find("0") or content.find("0") == -1:
                        score = 1.0
                elif "0" in content:
                    score = 0.0
                else:
                    # Try to parse as number
                    numbers = re.findall(r'\d+', content)
                    if numbers:
                        score = 1.0 if int(numbers[0]) == 1 else 0.0
            except:
                # Fallback: check if response indicates correctness
                content_lower = content.lower()
                if any(word in content_lower for word in ["correct", "right", "yes", "1"]):
                    if not any(word in content_lower for word in ["incorrect", "wrong", "no", "0"]):
                        score = 1.0
            
            # Special case: if predicted answer is empty or "0", score should be 0
            if not predicted or predicted.strip() == "0" or predicted.strip() == "":
                score = 0.0
            
            return score, {"correctness": int(score), "response": content}
            
        except Exception as e:
            return 0.0, {"error": str(e)}

    def _mmlongbench_doc_lasj(self, predicted: str, ground_truth: str, question: str = "") -> tuple:
        """MMLongBench-Doc official evaluation method (LasJ)"""
        if self._client is None:
            self._client = create_openai_client(api_key=self.api_key, base_url=self.base_url)
        
        # Load prompt from file (internal function)
        def _load_prompt() -> str:
            """Load MMLongBench-Doc evaluation prompt from file"""
            current_file = Path(__file__).resolve()
            project_root = current_file.parents[2]  # Go up from rollout/core/ to project root
            prompt_path = project_root / "projects" / "DocDancer" / "eval" / "LasJ_prompt_for_MMLongDocBench.md"
            
            # Also try relative to current working directory
            if not prompt_path.exists():
                prompt_path = Path("projects/DocDancer/eval/LasJ_prompt_for_MMLongDocBench.md")
            
            # Try absolute path from workspace
            if not prompt_path.exists():
                possible_paths = [
                    Path("projects/DocDancer/eval/LasJ_prompt_for_MMLongDocBench.md"),
                    Path("../projects/DocDancer/eval/LasJ_prompt_for_MMLongDocBench.md"),
                    project_root / "projects" / "DocDancer" / "eval" / "LasJ_prompt_for_MMLongDocBench.md",
                ]
                for path in possible_paths:
                    if path.exists():
                        prompt_path = path
                        break
            
            if not prompt_path.exists():
                raise FileNotFoundError(
                    f"MMLongBench-Doc prompt file not found. Tried: {prompt_path}\n"
                    f"Please ensure the file exists at: projects/DocDancer/eval/LasJ_prompt_for_MMLongDocBench.md"
                )
            
            with open(prompt_path, 'r', encoding='utf-8') as f:
                return f.read()
        
        # Load prompt from file
        try:
            prompt = _load_prompt()
        except FileNotFoundError as e:
            return 0.0, {"error": str(e)}
        
        # Construct user content in MMLongBench-Doc format
        user_content = "\n\nQuestion:{}\nAnswer:{}\nPrediction:{}\n".format(
            question, ground_truth, predicted
        )
        
        try:
            # MMLongBench-Doc uses two messages: prompt (user) + user_content (assistant)
            response = chat_completion(
                self._client,
                max_retries=self.max_retries,
                model=self.model_name,
                messages=[
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": user_content}
                ],
                temperature=self.temperature,
                **self.extra_params,
            )
            
            llm_output = response.choices[0].message.content
            
            # Extract reason and result from response
            # 1. Extract reasoning (eval_think)
            reason_match = re.search(r"reason:\s*(.*?)(?=\s*result:|$)", llm_output, re.IGNORECASE | re.DOTALL)
            
            if reason_match:
                eval_think = reason_match.group(1).strip()
            else:
                eval_think = f"Reasoning key 'reason:' not found. Raw output snippet: {llm_output[:150]}..."
            
            # 2. Extract result (YES/NO -> score)
            answer_match = re.search(r"result:\s*(YES|NO)", llm_output, re.IGNORECASE)
            
            score = 0.0  # Default to 0.0 (NO)
            if answer_match:
                answer_text = answer_match.group(1).strip().upper()
                if answer_text == "YES":
                    score = 1.0
            
            return score, {
                "correctness": int(score),
                "reasoning": eval_think,
                "raw_response": llm_output
            }
            
        except Exception as e:
            return 0.0, {"error": str(e)}

    def _mmlongbench_doc_f1(self, predicted: str, ground_truth: str, question: str = "", answer_format: str = "") -> tuple:
        """MMLongBench-Doc official F1-score metric"""
        # Dynamically import eval_score when needed
        def _load_eval_score():
            """Load eval_score function from projects/DocDancer/eval/eval_score.py"""
            current_file = Path(__file__).resolve()
            project_root = current_file.parents[2]  # Go up from rollout/core/ to project root
            eval_score_path = project_root / "projects" / "DocDancer" / "eval" / "eval_score.py"
            
            if not eval_score_path.exists():
                return None
            
            try:
                # Add parent directory to path
                eval_dir = str(eval_score_path.parent)
                if eval_dir not in sys.path:
                    sys.path.insert(0, eval_dir)
                import importlib.util
                spec = importlib.util.spec_from_file_location("eval_score", eval_score_path)
                eval_score_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(eval_score_module)
                return eval_score_module.eval_score
            except Exception:
                return None
        
        # Load answer extraction prompt (internal function)
        def _load_extractor_prompt() -> str:
            """Load answer extraction prompt from file"""
            current_file = Path(__file__).resolve()
            project_root = current_file.parents[2]  # Go up from rollout/core/ to project root
            prompt_path = project_root / "projects" / "DocDancer" / "eval" / "prompt_for_answer_extraction.md"
            
            # Also try relative to current working directory
            if not prompt_path.exists():
                prompt_path = Path("projects/DocDancer/eval/prompt_for_answer_extraction.md")
            
            # Try absolute path from workspace
            if not prompt_path.exists():
                possible_paths = [
                    Path("projects/DocDancer/eval/prompt_for_answer_extraction.md"),
                    Path("../projects/DocDancer/eval/prompt_for_answer_extraction.md"),
                    project_root / "projects" / "DocDancer" / "eval" / "prompt_for_answer_extraction.md",
                ]
                for path in possible_paths:
                    if path.exists():
                        prompt_path = path
                        break
            
            if not prompt_path.exists():
                raise FileNotFoundError(
                    f"Answer extraction prompt file not found. Tried: {prompt_path}\n"
                    f"Please ensure the file exists at: projects/DocDancer/eval/prompt_for_answer_extraction.md"
                )
            
            with open(prompt_path, 'r', encoding='utf-8') as f:
                return f.read()
        
        # Extract answer from model output using LLM (internal function)
        def _extract_answer(question: str, output: str, model_name: Optional[str] = None) -> str:
            """Extract answer from model output using LLM"""
            if self._client is None:
                self._client = create_openai_client(api_key=self.api_key, base_url=self.base_url)
            
            # Load prompt
            try:
                prompt = _load_extractor_prompt()
            except FileNotFoundError as e:
                return "Failed to load prompt"
            
            # Use provided model_name or default
            extractor_model = model_name or "gpt-4o-mini"
            
            # Construct user content in MMLongBench-Doc format
            user_content = "\n\nQuestion:{}\nAnalysis:{}\n".format(question, output)
            
            try:
                response = chat_completion(
                    self._client,
                    max_retries=self.max_retries,
                    model=extractor_model,
                    messages=[
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": user_content}
                    ],
                    temperature=0.0,
                    max_tokens=256,
                    **self.extra_params,
                )
                return response.choices[0].message.content
            except Exception as e:
                return f"Failed to extract answer: {str(e)}"
        
        # Load eval_score function
        eval_score = _load_eval_score()
        if eval_score is None:
            return 0.0, {"error": "eval_score module not found"}
        
        # Extract answer from prediction
        extracted_res = _extract_answer(question, predicted)
        
        # Parse extracted answer
        try:
            # Extract answer from format: "Extracted answer: [answer]\nAnswer format: [format]"
            if "Extracted answer:" in extracted_res:
                pred_ans = extracted_res.split("Answer format:")[0].split("Extracted answer:")[1].strip()
            else:
                pred_ans = extracted_res.strip()
            
            # Get answer format (use provided or try to extract from response)
            if not answer_format and "Answer format:" in extracted_res:
                format_part = extracted_res.split("Answer format:")[1].strip()
                answer_format = format_part.split("\n")[0].strip()
            
            # Normalize answer format (Int -> Int, etc.)
            if answer_format:
                # Map common formats
                format_map = {"Integer": "Int", "Float": "Float", "String": "Str", "List": "List", "None": "None"}
                answer_format = format_map.get(answer_format, answer_format)
            else:
                answer_format = "Str"  # Default
            
            # Calculate score using eval_score
            score = eval_score(ground_truth, pred_ans, answer_format)
            
            return score, {
                "extracted_answer": pred_ans,
                "answer_format": answer_format,
                "extraction_response": extracted_res,
                "ground_truth": ground_truth
            }
        except Exception as e:
            return 0.0, {
                "error": f"Failed to evaluate: {str(e)}",
                "extraction_response": extracted_res
            }

    def _mmlongbench_doc_acc(self, predicted: str, ground_truth: str, question: str = "", answer_format: str = "") -> tuple:
        """MMLongBench-Doc official Accuracy metric"""
        # Accuracy uses the same logic as F1, just different aggregation
        # Reuse the same implementation
        return self._mmlongbench_doc_f1(predicted, ground_truth, question=question, answer_format=answer_format)

    def _calculate_mmlongbench_f1(self, evaluations: List[EvaluationResult]) -> float:
        """Calculate MMLongBench-Doc F1-score from evaluation results
        
        Following the original eval_acc_and_f1 logic:
        - recall = sum(scores for answerable GT) / len(answerable GT)
        - precision = sum(scores for answerable GT) / len(answerable predictions)
        """
        if not evaluations:
            return 0.0
        
        # Filter answerable samples (ground truth is not "Not answerable")
        answerable_samples = [
            e for e in evaluations 
            if e.ground_truth and e.ground_truth.strip().lower() != "not answerable"
        ]
        
        if not answerable_samples:
            return 0.0
        
        # Calculate recall: sum of scores for answerable GT / count of answerable GT
        recall = sum(e.score for e in answerable_samples) / len(answerable_samples)
        
        # Calculate precision: sum of scores for answerable GT / count of predicted answerable
        # Get all predicted answerable (not "Not answerable" or "Fail to answer")
        predicted_answerable = [
            e for e in evaluations
            if e.details.get("extracted_answer", "").strip().lower() not in ["not answerable", "fail to answer", ""]
        ]
        
        if not predicted_answerable:
            return 0.0
        
        # Precision: sum of scores for answerable GT / count of predicted answerable
        # Note: numerator uses answerable_samples (same as recall), denominator uses predicted_answerable
        precision = sum(e.score for e in answerable_samples) / len(predicted_answerable)
        
        # Calculate F1
        if recall + precision == 0.0:
            return 0.0
        
        f1 = 2 * recall * precision / (recall + precision)
        return f1


def evaluate_results(
    results: List[TaskResult],
    metric: str = "exact_match",
    model_name: str = "gpt-4.1-2025-04-14",
    api_key: str = "",
    base_url: str = "",
    temperature: float = 0.0,
    max_retries: int = 3,
    extra_params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Convenience function to evaluate results.
    
    Args:
        results: List of task results
        metric: Evaluation metric
        model_name: Model for LLM-based evaluation
        api_key: API key from rollout config
        base_url: API base URL from rollout config
        temperature: Sampling temperature for llm_judgement
        max_retries: Retry attempts for llm_judgement
        extra_params: Extra completion params for llm_judgement
        
    Returns:
        Evaluation summary
    """
    evaluator = Evaluator(
        metric=metric,
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        max_retries=max_retries,
        extra_params=extra_params,
    )
    return evaluator.evaluate(results)
