"""Create a RANDOM tiny Qwen3 locally, without downloading any weights.

This tests engineering behavior only. It is not Qwen3-8B or a useful SQL model.
"""
from __future__ import annotations
import argparse
from pathlib import Path


def create_smoke_model(output):
    import torch
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import PreTrainedTokenizerFast, Qwen3Config, Qwen3ForCausalLM
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'config.json').exists():
        raise FileExistsError(f'refusing to overwrite an existing model: {output}')
    vocabulary = ['[UNK]', '<|endoftext|>', '[PAD]', '{', '}', ':', ',', '"',
                  'kind', 'query', 'clarify', 'unsupported', 'reason_code', 'write_not_allowed']
    tokenizer = Tokenizer(WordLevel({text: i for i, text in enumerate(vocabulary)}, unk_token='[UNK]'))
    tokenizer.pre_tokenizer = Whitespace()
    fast = PreTrainedTokenizerFast(tokenizer_object=tokenizer, unk_token='[UNK]',
        eos_token='<|endoftext|>', pad_token='[PAD]', model_max_length=2048)
    torch.manual_seed(17)
    model = Qwen3ForCausalLM(Qwen3Config(vocab_size=len(vocabulary), hidden_size=32,
        intermediate_size=64, num_hidden_layers=1, num_attention_heads=4,
        num_key_value_heads=2, head_dim=8, max_position_embeddings=2048,
        eos_token_id=fast.eos_token_id, pad_token_id=fast.pad_token_id,
        attention_dropout=0.0, tie_word_embeddings=False))
    model.save_pretrained(output)
    fast.save_pretrained(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='outputs/tiny-qwen3-smoke')
    args = parser.parse_args()
    print(create_smoke_model(args.output))


if __name__ == '__main__':
    main()
