from typing import Tuple

from transformers import (
    AutoModelForCausalLM, LlamaForCausalLM, Gemma2ForCausalLM
)


def model_supported(model: AutoModelForCausalLM) -> Tuple[bool, str]:
    if isinstance(model, LlamaForCausalLM):
        return True, "Llama31"
    elif isinstance(model, Gemma2ForCausalLM):
        return True, "Gemma2"
    else:
        return False, None


def llama_post_process(text: str) -> str:
    text = text.split("<|end_header_id|>")[-1]
    text = text.replace("<|eot_id|>","")

    return text
