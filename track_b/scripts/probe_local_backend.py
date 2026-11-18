#!/usr/bin/env python3
"""Offline random-weight LoRA compatibility probe, NOT an extraction experiment.

Use an external timeout (e.g. timeout 300) to bound GPU time. No weights,
tokenizer, datasets, or model services are downloaded by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4

from artifacts import run_directory
from config_utils import DEFAULT_CONFIG, load_config
from train_lora import resolve_args, training_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-config', type=Path, required=True)
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--rank', type=int, default=8)
    parser.add_argument('--max-seq', type=int, default=512)
    parser.add_argument('--steps', type=int, default=2)
    parser.add_argument('--seed', type=int, default=1337)
    parser.add_argument('--out', type=Path, default=None)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if min(args.rank, args.max_seq, args.steps) <= 0:
        parser.error('rank, sequence length and steps must be positive')
    raw = args.model_config.read_bytes()
    model_config = json.loads(raw)
    if model_config.get('model_type') != 'qwen2':
        parser.error('only a local Qwen2 architecture config is supported')
    config = load_config(args.config)
    out = args.out or Path('results') / f'backend-probe-{uuid4().hex}'
    rc = resolve_args(['--rank', str(args.rank), '--max-seq', str(args.max_seq),
                       '--seed', str(args.seed), '--batch-size', '1', '--grad-accum', '1',
                       '--grad-ckpt', '--no-qlora',
                       '--out', str(out)], config)
    report = {
        'evidence_mode': 'random_weight_compatibility_only',
        'pretrained_weights_used': False, 'api_calls': 0,
        'model_config_sha256': hashlib.sha256(raw).hexdigest(),
        'training_config_sha256': hashlib.sha256(args.config.read_bytes()).hexdigest(),
        'probe_script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'train_script_sha256': hashlib.sha256(Path(__file__).with_name('train_lora.py').read_bytes()).hexdigest(),
        'rank': args.rank, 'max_seq': args.max_seq, 'steps_requested': args.steps,
        'seed': args.seed, 'batch_size': 1, 'gradient_accumulation_steps': 1,
        'target_modules': rc['target_modules'], 'status': 'planned',
    }
    if args.dry_run:
        print(json.dumps(report, indent=2))
        return 0
    started = time.perf_counter()
    with run_directory(out) as state:
        try:
            os.environ.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
            import torch
            from transformers import Qwen2Config, AutoModelForCausalLM, Trainer, TrainingArguments
            from peft import LoraConfig, get_peft_model, TaskType

            if not torch.cuda.is_available():
                raise RuntimeError('CUDA unavailable; this probe does not silently use CPU')
            report['versions'] = {name: importlib.metadata.version(name) for name in
                                  ('torch', 'transformers', 'peft', 'accelerate')}
            report.update(gpu=torch.cuda.get_device_name(0), cuda=torch.version.cuda,
                          bf16_supported=torch.cuda.is_bf16_supported(),
                          free_before_bytes=torch.cuda.mem_get_info()[0],
                          total_device_bytes=torch.cuda.mem_get_info()[1])
            torch.manual_seed(args.seed)
            torch.cuda.reset_peak_memory_stats()
            dtype = torch.bfloat16 if report['bf16_supported'] else torch.float16
            cfg = Qwen2Config.from_dict(model_config)
            cfg.use_cache = False
            with torch.device('cuda'):
                model = AutoModelForCausalLM.from_config(cfg, torch_dtype=dtype,
                                                        attn_implementation='sdpa')
            model.gradient_checkpointing_enable()
            model.enable_input_require_grads()
            model = get_peft_model(model, LoraConfig(
                r=args.rank, lora_alpha=2 * args.rank, lora_dropout=rc['dropout'],
                target_modules=rc['target_modules'], bias='none', task_type=TaskType.CAUSAL_LM))
            report.update(base_dtype=str(dtype), trainer_fp16=True,
                          parameters=sum(p.numel() for p in model.parameters()),
                          trainable_parameters=sum(p.numel() for p in model.parameters() if p.requires_grad))
            tracked = next(p for name, p in model.named_parameters() if 'lora_B' in name and p.requires_grad)
            before_update = tracked.detach().clone()
            # Synthetic token IDs only; deliberately no corpus, target canary or tokenizer.
            generator = torch.Generator(device='cpu').manual_seed(args.seed)
            rows = []
            for _ in range(args.steps):
                ids = torch.randint(100, min(1000, cfg.vocab_size), (args.max_seq,),
                                    generator=generator, device='cpu')
                rows.append({'input_ids': ids, 'attention_mask': torch.ones_like(ids), 'labels': ids.clone()})
            settings = training_settings(rc, fp16=True, grad_ckpt=True)
            settings.update(max_steps=args.steps, save_strategy='no', logging_steps=1,
                            disable_tqdm=True, dataloader_pin_memory=False, logging_nan_inf_filter=False)
            report['training_settings'] = {key: value for key, value in settings.items() if key != 'output_dir'}
            trainer = Trainer(model=model, args=TrainingArguments(**settings), train_dataset=rows)
            torch.cuda.synchronize()
            train_started = time.perf_counter()
            result = trainer.train()
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - train_started
            updated = not torch.equal(before_update, tracked.detach())
            finite = all(torch.isfinite(p).all().item() for p in model.parameters() if p.requires_grad)
            if (not math.isfinite(result.training_loss) or not finite or not updated
                    or trainer.state.global_step != args.steps
                    or trainer.accelerator.optimizer_step_was_skipped):
                raise RuntimeError('Probe requires finite loss/parameters and completed parameter updates')
            report.update(status='complete', optimizer_steps=trainer.state.global_step,
                          lora_parameter_updated=updated, trainable_parameters_finite=finite,
                          training_loss=result.training_loss, training_seconds=elapsed,
                          seconds_per_step=elapsed / trainer.state.global_step,
                          max_allocated_bytes=torch.cuda.max_memory_allocated(),
                          max_reserved_bytes=torch.cuda.max_memory_reserved(),
                          loss_history=trainer.state.log_history)
            state['status'] = 'complete'
        except Exception as error:
            report.update(status='failed', error_type=type(error).__name__, error=str(error))
            state['status'] = 'failed'
        finally:
            report['wall_seconds'] = time.perf_counter() - started
            with (out / 'probe.json').open('x', encoding='utf-8') as stream:
                json.dump(report, stream, indent=2)
        print(json.dumps(report, indent=2))
        return 0 if report['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())
