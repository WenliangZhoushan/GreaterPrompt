import csv
import importlib
import os
import time
from collections import defaultdict
from typing import List

from src.greaterprompt.core.PE2.cli import ape_apo_pe2_optimizer
from src.greaterprompt.dataloader import GreaterDataloader
from src.greaterprompt.models import model_supported
from src.greaterprompt.utils import ape_apo_pe2_args, clean_string

import torch
from torch.nn import functional as F
from tqdm import tqdm
from transformers import AutoModelForCausalLM, AutoTokenizer


class BaseOptimizer:
    def __init__(self, trainer: str, optimize_config: dict = {}):
        self.args = ape_apo_pe2_args(trainer)
        self.args.task_model = optimize_config.get("task_model", "openai_gpt35_turbo_instruct")
        self.args.optim_model = optimize_config.get("optim_model", "openai_gpt4_turbo")


    def write_data(self, inputs: GreaterDataloader, p_init: str) -> None:
        os.makedirs(self.args.data_dir, exist_ok=True)

        for filename in ["train", "test", "dev"]:
            with open(os.path.join(self.args.data_dir, f"{filename}.csv"), "w") as f:
                writer = csv.writer(f)
                writer.writerow(["", "input", "label"])

                for i, input in enumerate(inputs):
                    writer.writerow([i, input["question"], input["answer"]])
        
        with open(os.path.join(self.args.data_dir, "prompt.md"), "w") as f:
            f.write(p_init)
        
        return


    def optimize(self, inputs: GreaterDataloader, p_init: str) -> str:
        self.write_data(inputs, p_init)

        p_star = ape_apo_pe2_optimizer(self.args)

        return p_star


class ApeOptimizer(BaseOptimizer):
    def __init__(self, optimize_config: dict = {}):
        super().__init__("ape", optimize_config)


class ApoOptimizer(BaseOptimizer):
    def __init__(self, optimize_config: dict = {}):
        super().__init__("apo", optimize_config)


class GreaterOptimizer:
    def __init__(
            self, model: AutoModelForCausalLM, tokenizer: AutoTokenizer,
            optimize_config: dict = {}, *args, **kwargs
        ):
        self.optimize_config = optimize_config
        self._init_agents(model, tokenizer)
    

    def _init_agents(self, model: AutoModelForCausalLM, tokenizer: AutoTokenizer):
        supported, model_name = model_supported(model)
        assert supported, f"Model: {model} is not supported"

        model_class = getattr(importlib.import_module("greaterprompt.models"), model_name)
        self.client = model_class(model, tokenizer)

        for param in self.client.model.parameters():
            param.requires_grad = False
        self.client.model.get_input_embeddings().weight.requires_grad = True


    def encode_input(self, batch_inputs: List[dict], p_extractor: str) -> torch.Tensor:
        q_tokens, p_tokens, y_tokens = [], [], []
        for input in batch_inputs:
            q_token = self.client.tokenizer.encode(input["question"].strip() + " ?", return_tensors="pt")
            p_token = self.client.tokenizer.encode(" " + input["prompt"], return_tensors="pt")
            if self.client.model.config.model_type == "llama":
                y_token = self.client.tokenizer.encode(" " + input["answer"], return_tensors="pt")
            elif self.client.model.config.model_type == "gemma2":
                y_token = self.client.tokenizer.encode(input["answer"], return_tensors="pt")
            q_tokens.append(q_token)
            p_tokens.append(p_token[:, 1:])
            y_tokens.append(y_token[:, 1:])
        
        p_extr_token = self.client.tokenizer.encode(p_extractor, return_tensors="pt")
        p_extr_token = p_extr_token[:, 1:]

        return (
            [t.to(self.client.device, dtype=torch.long) for t in q_tokens],
            [t.to(self.client.device, dtype=torch.long) for t in p_tokens],
            p_extr_token.to(self.client.device, dtype=torch.long),
            [t.to(self.client.device, dtype=torch.long) for t in y_tokens]
        )
    

    def get_pred_probs(self, input: torch.Tensor, y_tokens: torch.Tensor) -> torch.Tensor:
        probs = []

        with torch.enable_grad():
            generate_config = self.optimize_config["generate_config"]
            for i in range(len(y_tokens)):
                logits = self.client.get_logits(input, generate_config)[:, -1, :]
                next_token_id = torch.argmax(logits, dim=-1)

                # ----------------- only for gemma2 ----------------- #
                # TODO: hardcode here, if gemma2 generate \n, keep generating until it is not
                if self.client.model.config.model_type == "gemma2":
                    while self.client.tokenizer.decode(next_token_id[0], skip_special_tokens=True) in ["\n", "\n\n"]:
                        input = torch.cat([input, next_token_id.unsqueeze(0)], dim=1)
                        logits = self.client.get_logits(input, generate_config)[:, -1, :]
                        next_token_id = torch.argmax(logits, dim=-1)
                # ----------------- only for gemma2 ----------------- #
    
                next_token_id = next_token_id.unsqueeze(0)
                probs.append(F.softmax(logits, dim=-1))
                input = torch.cat([input, next_token_id], dim=1)
                del logits
                if i % 5 == 0:
                    torch.cuda.empty_cache()

        return torch.cat(probs, dim=0)
    

    def perplexity_loss(self, q_tokens: torch.Tensor, p_tokens: torch.Tensor) -> torch.Tensor:
        loss = torch.tensor(0.0, device=p_tokens.device)
        
        for i in range(1, p_tokens.size(1)):
            self.client.model.zero_grad()
            input_ids = torch.cat([q_tokens, p_tokens[:, :i]], dim=1)
            logits = self.client.get_logits(input_ids, self.optimize_config["generate_config"])[-1, -1, :]
            log_probs = F.log_softmax(logits, dim=-1)
            loss += log_probs[p_tokens[0, i]]
        
        loss = torch.exp(-1 * loss / p_tokens.size(1))
        return loss


    def calculate_loss(self, q_tokens: torch.Tensor, p_tokens: torch.Tensor, y_hat: torch.Tensor, y: torch.Tensor) -> torch.Tensor:
        self.client.model.zero_grad()

        loss_function = self.optimize_config.get("loss_function", F.cross_entropy)
        if self.optimize_config.get("perplexity_loss", False):
            perpl_lambda = self.optimize_config.get("perplexity_lambda", 0.2)
        else:
            perpl_lambda = 0
        
        raw_loss = loss_function(y_hat, y)
        perpl_loss = perpl_lambda * self.perplexity_loss(q_tokens, p_tokens)
        loss = raw_loss + perpl_loss
        loss.backward()

        return raw_loss


    def get_gradients(self, q_tokens: torch.Tensor, p_tokens: torch.Tensor, y_tokens: torch.Tensor, y_hat_probs: torch.Tensor) -> List[torch.Tensor]:
        gradients = []

        for y, y_hat in zip(y_tokens[0, :], y_hat_probs):
            loss = self.calculate_loss(q_tokens, p_tokens, y_hat, y)
            embedding_layer = self.client.model.get_input_embeddings()
            embedding_grad = embedding_layer.weight.grad.detach().clone()
            gradients.append(embedding_grad.cpu())

        return gradients, loss


    def get_candidates(self, input: torch.Tensor) -> List[int]:
        with torch.inference_mode():
            candidates = self.client.get_candidates(input, self.optimize_config)

        return candidates
    

    def get_reasoning(self, input: torch.Tensor) -> str:
        generate_config = self.optimize_config.get("generate_config", {})
        with torch.inference_mode():
            response = self.client.generate(input, generate_config)

        return response
    
    
    def get_p_i_star(self, gradients: List[torch.Tensor], candidates: List[int]) -> int:
        p_i_star, p_i_star_grad = None, float("-inf")

        for candidate in candidates:
            token_grad = sum([torch.norm(grad[candidate], p=2) * -1 for grad in gradients])
            token_grad /= len(gradients)
            if token_grad > p_i_star_grad:
                p_i_star = candidate
                p_i_star_grad = token_grad

        return p_i_star


    def optimize(self, inputs: GreaterDataloader, p_extractor: str, rounds: int) -> dict[str, List[str]]:
        outputs = defaultdict(list)

        intersect_q = self.optimize_config.get("intersect_q", 1)
        batch_inputs = [inputs[i:i+intersect_q] for i in range(0, len(inputs), intersect_q)]

        for i, batch in enumerate(batch_inputs):
            question_tokens, p_tokens, p_extr_tokens, y_tokens = self.encode_input(batch, p_extractor)
            assert all(p_token.size(1) >= 2 for p_token in p_tokens), "Init prompt should be at least 2 tokens"
            p_stars = defaultdict(list)
            idx = 1
            truncated = [False] * len(batch)

            start_time = time.time()
            for j in tqdm(range(rounds), desc=f"Optimizing {i + 1} / {len(batch_inputs)}"):
                torch.cuda.empty_cache()
                # get candidates for p_i by using x + p_0 ... p_i-1 for each p in the batch
                candidates_tmp = []

                for k, p in enumerate(p_tokens):
                    if idx >= p.size(1): continue
                    p_token_i = p[:, idx]
                    input_ids = torch.cat([question_tokens[k], p[:, :idx]], dim=1)
                    p_candidates = set(self.get_candidates(input_ids))
                    # avoid adding dummy token to candidates
                    eos_token_id = self.client.tokenizer.eos_token_id
                    p_candidates.update([int(p_token_i[0])] if int(p_token_i[0]) != eos_token_id else [])
                    candidates_tmp.append(p_candidates)
                
                candidates = set(candidates_tmp[0])
                for c in candidates_tmp[1:]:
                    candidates = candidates.intersection(c)
                if not candidates:
                    candidates = set(t for c in candidates_tmp for t in c)
                
                # use intersection candidates to optimize each p in the batch
                for k, p in enumerate(p_tokens):
                    if idx >= p.size(1): continue
                    # use x + p to get reasoning chain r
                    input_ids = torch.cat([question_tokens[k], p], dim=1)
                    reasoning_chain = self.get_reasoning(input_ids)
                    r_tokens = self.client.tokenizer.encode(reasoning_chain, return_tensors="pt")
                    r_tokens = r_tokens.to(self.client.device)

                    # use x + p + r + p_extractor to get logits of y_hat(x and p is already included in r)
                    input_ids = torch.cat([r_tokens, p_extr_tokens], dim=1)
                    y_hat_probs = self.get_pred_probs(input_ids, y_tokens[k])
                    gradients, loss = self.get_gradients(question_tokens[k], p[:, :idx], y_tokens[k], y_hat_probs)

                    # calculate gradient for each candidate to get p_i_star
                    p_i_star = self.get_p_i_star(gradients, candidates)
                    p[:, idx] = p_i_star

                    # if p_i_star is a period, truncate the prompt and star from the beginning
                    p_i_star_token = self.client.tokenizer.decode(p_i_star)
                    if p_i_star_token.strip() in ".?!:":
                        p_tokens[k] = p[:, :idx + 1]
                        truncated[k] = True
                        decoded_p_star = self.client.tokenizer.decode(p_tokens[k][0, :], skip_special_tokens=True)
                        p_stars[batch[k]["question"]].append((repr(decoded_p_star.strip()), loss.item()))
                    # add a dummy token to the end of the prompt for next round of candidate generation
                    elif p_i_star_token.strip() != "." and idx == len(p[0, :]) - 1:
                        eos_token_id = self.client.tokenizer.eos_token_id
                        dummy_token = torch.tensor([[eos_token_id]], device=p.device)
                        p_tokens[k] = torch.cat([p, dummy_token], dim=1)

                if all(t for t in truncated):
                    idx = 1
                    truncated = [False] * len(batch)
                else:
                    idx = (idx + 1) % max(p.size(1) for p in p_tokens)
            
            # save and filter the optimized prompts
            outputs.update(p_stars)
            for k, p in enumerate(p_tokens):
                outputs[batch[k]["question"]].append((
                    repr(self.client.tokenizer.decode(p[0, :], skip_special_tokens=True).strip()),
                    loss.item()
                ))
            for question, prompts in outputs.items():
                cleaned_prompts = clean_string(prompts)
                if self.optimize_config.get("filter", False):
                    outputs[question] = self.client.filter(cleaned_prompts)

            del input_ids, reasoning_chain, r_tokens, y_hat_probs
            torch.cuda.empty_cache()

        return outputs


    def optimize_streamlit(self, inputs: GreaterDataloader, p_extractor: str, rounds: int, callback=None) -> dict[str, List[str]]:
        outputs = defaultdict(list)

        intersect_q = self.optimize_config.get("intersect_q", 1)
        batch_inputs = [inputs[i:i+intersect_q] for i in range(0, len(inputs), intersect_q)]

        total_batches = len(batch_inputs)

        for i, batch in enumerate(batch_inputs):
            question_tokens, p_tokens, p_extr_tokens, y_tokens = self.encode_input(batch, p_extractor)
            assert all(p_token.size(1) >= 2 for p_token in p_tokens), "Init prompt should be at least 2 tokens"
            p_stars = defaultdict(list)
            idx = 1
            truncated = [False] * len(batch)

            start_time = time.time()
            for j in range(rounds):
                if callback:
                    batch_progress = (i / total_batches)
                    round_progress = (j / rounds) / total_batches
                    total_progress = batch_progress + round_progress
                    
                    current_loss = None
                    if p_stars:
                        for question in p_stars:
                            if p_stars[question]:
                                _, loss_value = p_stars[question][-1]
                                if current_loss is None or loss_value < current_loss:
                                    current_loss = loss_value
                    
                    status_info = {
                        "batch": i + 1,
                        "total_batches": total_batches,
                        "round": j + 1,
                        "total_rounds": rounds,
                        "loss": current_loss
                    }
                    
                    callback(total_progress, status_info)
                
                torch.cuda.empty_cache()
                # get candidates for p_i by using x + p_0 ... p_i-1 for each p in the batch
                candidates_tmp = []

                for k, p in enumerate(p_tokens):
                    if idx >= p.size(1): continue
                    p_token_i = p[:, idx]
                    input_ids = torch.cat([question_tokens[k], p[:, :idx]], dim=1)
                    p_candidates = set(self.get_candidates(input_ids))
                    # avoid adding dummy token to candidates
                    eos_token_id = self.client.tokenizer.eos_token_id
                    p_candidates.update([int(p_token_i[0])] if int(p_token_i[0]) != eos_token_id else [])
                    candidates_tmp.append(p_candidates)
                
                candidates = set(candidates_tmp[0])
                for c in candidates_tmp[1:]:
                    candidates = candidates.intersection(c)
                if not candidates:
                    candidates = set(t for c in candidates_tmp for t in c)
                
                # use intersection candidates to optimize each p in the batch
                for k, p in enumerate(p_tokens):
                    if idx >= p.size(1): continue
                    # use x + p to get reasoning chain r
                    input_ids = torch.cat([question_tokens[k], p], dim=1)
                    reasoning_chain = self.get_reasoning(input_ids)
                    r_tokens = self.client.tokenizer.encode(reasoning_chain, return_tensors="pt")
                    r_tokens = r_tokens.to(self.client.device)

                    # use x + p + r + p_extractor to get logits of y_hat(x and p is already included in r)
                    input_ids = torch.cat([r_tokens, p_extr_tokens], dim=1)
                    y_hat_probs = self.get_pred_probs(input_ids, y_tokens[k])
                    gradients, loss = self.get_gradients(question_tokens[k], p[:, :idx], y_tokens[k], y_hat_probs)

                    # calculate gradient for each candidate to get p_i_star
                    p_i_star = self.get_p_i_star(gradients, candidates)
                    p[:, idx] = p_i_star

                    # if p_i_star is a period, truncate the prompt and star from the beginning
                    p_i_star_token = self.client.tokenizer.decode(p_i_star)
                    if p_i_star_token.strip() in ".?!:":
                        p_tokens[k] = p[:, :idx + 1]
                        truncated[k] = True
                        decoded_p_star = self.client.tokenizer.decode(p_tokens[k][0, :], skip_special_tokens=True)
                        p_stars[batch[k]["question"]].append((repr(decoded_p_star.strip()), loss.item()))
                    # add a dummy token to the end of the prompt for next round of candidate generation
                    elif p_i_star_token.strip() != "." and idx == len(p[0, :]) - 1:
                        eos_token_id = self.client.tokenizer.eos_token_id
                        dummy_token = torch.tensor([[eos_token_id]], device=p.device)
                        p_tokens[k] = torch.cat([p, dummy_token], dim=1)

                if all(t for t in truncated):
                    idx = 1
                    truncated = [False] * len(batch)
                else:
                    idx = (idx + 1) % max(p.size(1) for p in p_tokens)
            
            # save and filter the optimized prompts
            outputs.update(p_stars)
            for k, p in enumerate(p_tokens):
                outputs[batch[k]["question"]].append((
                    repr(self.client.tokenizer.decode(p[0, :], skip_special_tokens=True).strip()),
                    loss.item()
                ))
            for question, prompts in outputs.items():
                cleaned_prompts = clean_string(prompts)
                if self.optimize_config.get("filter", False):
                    outputs[question] = self.client.filter(cleaned_prompts)

            del input_ids, reasoning_chain, r_tokens, y_hat_probs
            torch.cuda.empty_cache()

        if callback:
            callback(1.0, {"status": "complete"})

        return outputs


class Pe2Optimizer(BaseOptimizer):
    def __init__(self, optimize_config: dict = {}):
        super().__init__("pe2", optimize_config)
