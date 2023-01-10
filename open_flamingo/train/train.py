""" Main training script """

import argparse
import glob
import os
import random

import numpy as np
import torch
import wandb
from torch.nn.parallel import DistributedDataParallel as DDP
from transformers import get_constant_schedule_with_warmup

from data import get_data
from distributed import init_distributed_device, world_info_from_env
from eval.evaluate import evaluate_coco, evaluate_vqa
from open_flamingo import create_model_and_transforms
from train_utils import train_one_epoch, get_checkpoint


def random_seed(seed=42, rank=0):
    torch.manual_seed(seed + rank)
    np.random.seed(seed + rank)
    random.seed(seed + rank)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vision_encoder_path", default="openai/clip-vit-large-patch14", type=str)
    parser.add_argument(
        "--clip_processor_path", default=None, type=str, help="path to clip processor defaults to vision_encoder_path"
    )
    parser.add_argument("--lm_path", default="facebook/opt-1.3b", type=str)

    # From previous experiments other opt tokenizers may have a bug
    # so we defualt to this one in any case they should all be the same.
    parser.add_argument("--tokenizer_path", default="facebook/opt-30b", type=str, help="path to tokenizer")
    parser.add_argument(
        "--run_name", type=str, default="large model test", help="used to name saving directory and wandb run"
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--num_epochs", type=int, default=1)
    parser.add_argument("--batch_size", type=int, default=128)
    parser.add_argument("--gradient_accumulation_steps", type=int, default=1)
    parser.add_argument("--do_eval", action="store_true")
    parser.add_argument("--resume_from_checkpoint", type=str, default=None)
    parser.add_argument(
        "--delete_previous_checkpoint",
        action="store_true",
        help="delete previous checkpoint when saving new checkpoint",
    )
    parser.add_argument("--shards", type=str, default="/data/yfcc-tmp/cah/shards/shard_{000000..053008}.tar")
    parser.add_argument(
        "--dataset_type",
        type=str,
        default="image_text",
        choices=["image_text", "interleaved"],
        help="use image_text for LAION",
    )
    parser.add_argument("--eval_coco_data_dir", type=str, default="/data/yfcc-tmp/data/mscoco")
    parser.add_argument("--eval_okvqa_data_dir", type=str, default="/mmfs1/gscratch/efml/anasa2/data/ok-vqa/train")
    parser.add_argument("--eval_vqav2_data_dir", type=str, default="/mmfs1/gscratch/efml/anasa2/data/vqav2/train2014/")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--learning_rate", default=1e-4, type=float)
    parser.add_argument("--warmup_steps", default=5000, type=int)
    parser.add_argument("--weight_decay", default=0.1, type=float)
    parser.add_argument(
        "--precision",
        choices=["amp_bf16", "amp_bfloat16", "bf16", "fp16", "fp32"],
        default="fp32",
        help="Floating point precision.",
    )
    # data args
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--train_num_samples", type=int, default=None)
    parser.add_argument("--dataset_resampled", action="store_true")
    # distributed training args
    parser.add_argument(
        "--dist-url",
        default="env://",
        type=str,
        help="url used to set up distributed training",
    )
    parser.add_argument("--dist-backend", default="nccl", type=str, help="distributed backend")
    parser.add_argument("--horovod", default=False, action="store_true", help="Use horovod for distributed training.")
    parser.add_argument(
        "--no-set-device-rank",
        default=False,
        action="store_true",
        help="Don't set device index from local rank (when CUDA_VISIBLE_DEVICES restricted to one per proc).",
    )
    # wandb args
    parser.add_argument("--report_to_wandb", default=False, action="store_true")
    parser.add_argument(
        "--wandb_project",
        default="open-flamingo",
        type=str,
    )
    parser.add_argument(
        "--wandb_entity",
        default="anas-awadalla",
        type=str,
    )

    # if torch.cuda.is_available():
    #   # This enables tf32 on Ampere GPUs which is only 8% slower than
    #   # float16 and almost as accurate as float32
    #   # This was a default in pytorch until 1.12
    #   torch.backends.cuda.matmul.allow_tf32 = True
    #   torch.backends.cudnn.benchmark = True
    #   torch.backends.cudnn.deterministic = False

    args = parser.parse_args()

    if args.offline:
        os.environ["WANDB_MODE"] = "offline"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"

    args.local_rank, args.rank, args.world_size = world_info_from_env()

    device_id = init_distributed_device(args)

    random_seed(args.seed)

    model, image_processor, tokenizer = create_model_and_transforms(
        args.vision_encoder_path,
        args.clip_processor_path if args.clip_processor_path else args.vision_encoder_path,
        args.lm_path,
        args.tokenizer_path if args.tokenizer_path else args.lm_path,
        use_local_files=args.offline,
    )

    random_seed(args.seed, args.rank)

    print(f"Start running training on rank {args.rank}.")

    if args.rank == 0 and args.report_to_wandb:
        wandb.init(project=args.wandb_project, entity=args.wandb_entity, name=args.run_name, config=vars(args))

    device_id = args.rank % torch.cuda.device_count()
    model = model.to(device_id)

    ddp_model = DDP(model, device_ids=[device_id])

    train_dataset = get_data(args, image_processor, tokenizer)

    def get_grouped_params(model):
        params_with_wd, params_without_wd = [], []

        def apply_decay(x):
            return (
                "gated_cross_attn_layer" in x
                and "ff_gate" not in x
                and "attn_gate" not in x
                and "norm" not in x
                and "bias" not in x
            )

        for n, p in model.named_parameters():
            if apply_decay(n):
                params_with_wd.append(p)
            else:
                params_without_wd.append(p)
        return [
            {"params": params_with_wd, "weight_decay": args.weight_decay},
            {"params": params_without_wd, "weight_decay": 0.0},
        ]

    optimizer = torch.optim.AdamW(get_grouped_params(ddp_model), lr=args.learning_rate)
    lr_scheduler = get_constant_schedule_with_warmup(optimizer, num_warmup_steps=args.warmup_steps)

    # check if a checkpoint exists for this run
    if os.path.exists(f"{args.run_name}") and args.resume_from_checkpoint is None:
        checkpoint_list = glob.glob(f"{args.run_name}/checkpoint_*.pt")
        if len(checkpoint_list) == 0:
            print(f"Found no checkpoints for run {args.run_name}.")
        else:
            args.resume_from_checkpoint = sorted(checkpoint_list, key=lambda x: int(x.split("_")[-1].split(".")[0]))[-1]
            print(f"Found checkpoint {args.resume_from_checkpoint} for run {args.run_name}.")

    resume_from_epoch = 0
    if args.resume_from_checkpoint is not None:
        print(f"Loading checkpoint from {args.resume_from_checkpoint}")
        checkpoint = torch.load(args.resume_from_checkpoint, map_location="cpu")
        ddp_model.load_state_dict(checkpoint["model_state_dict"], False)
        optimizer.load_state_dict(checkpoint["optimizer_state_dict"])
        lr_scheduler.load_state_dict(checkpoint["lr_scheduler_state_dict"])
        resume_from_epoch = checkpoint["epoch"] + 1

    ddp_model.train()

    for epoch in range(resume_from_epoch, args.num_epochs):
        train_dataset.set_epoch(epoch)
        train_loader = train_dataset.dataloader

        train_one_epoch(
            args=args,
            model=ddp_model,
            epoch=epoch,
            tokenizer=tokenizer,
            optimizer=optimizer,
            lr_scheduler=lr_scheduler,
            train_loader=train_loader,
            device_id=device_id,
            wandb=wandb,
        )

        if args.do_eval and args.rank == 0:
            step = wandb.run.step if args.report_to_wandb else 0
            score = evaluate_coco(
                ddp_model,
                tokenizer,
                image_processor,
                data_dir=args.eval_coco_data_dir,
                batch_size=args.batch_size,
                num_samples=5000,
                num_shots=0,
                device=device_id,
                wandb=wandb if args.report_to_wandb else None,
                step=step,
            )

            if args.report_to_wandb:
                wandb.log(score, step=step, commit=False)

            vqa_score = evaluate_vqa(
                ddp_model,
                tokenizer,
                image_processor,
                benchmark_name="OKVQA",
                data_dir=args.eval_okvqa_data_dir,
                batch_size=args.batch_size,
                num_samples=5000,
                num_shots=0,
                device=device_id,
                wandb=wandb if args.report_to_wandb else None,
                step=step,
            )

            if args.report_to_wandb:
                wandb.log(vqa_score, step=step, commit=False)

            vqa_score = evaluate_vqa(
                ddp_model,
                tokenizer,
                image_processor,
                benchmark_name="VQAv2",
                data_dir=args.eval_vqav2_data_dir,
                batch_size=args.batch_size,
                num_samples=5000,
                num_shots=0,
                device=device_id,
                wandb=wandb if args.report_to_wandb else None,
                step=step,
            )

            if args.report_to_wandb:
                wandb.log(vqa_score, step=step, commit=True)

            ddp_model.train()

        if args.rank == 0:
            if not os.path.exists(args.run_name):
                os.makedirs(args.run_name)

            checkpoint_dict = {
                "epoch": epoch,
                "model_state_dict": get_checkpoint(ddp_model),
                "optimizer_state_dict": optimizer.state_dict(),
                "lr_scheduler_state_dict": lr_scheduler.state_dict(),
            }

            torch.save(checkpoint_dict, f"{args.run_name}/checkpoint_{epoch}.pt")
            if args.report_to_wandb:
                wandb.save(f"{args.run_name}/checkpoint_{epoch}.pt")

            if args.delete_previous_checkpoint:
                if epoch > 0:
                    os.remove(f"{args.run_name}/checkpoint_{epoch-1}.pt")

    if args.rank == 0:
        if not os.path.exists(args.run_name):
            os.makedirs(args.run_name)
        torch.save(get_checkpoint(ddp_model), f"{args.run_name}/final_weights.pt")
        if args.report_to_wandb:
            wandb.save(f"{args.run_name}/final_weights.pt")


if __name__ == "__main__":
    main()