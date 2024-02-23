import os
from pathlib import Path
from datetime import datetime

import torch
from torch import nn
from torch.utils.data import DataLoader
from fastai.vision.all import (
    Learner, DataLoaders, RocAuc, RocAucBinary, F1Score, AccumMetric, 
    Precision, Recall, accuracy, SaveModelCallback, CSVLogger, EarlyStoppingCallback,
)
from transformers import AutoImageProcessor

from data import get_augmentation, HistoCRCDataset
from utils import load_model, validate, validate_binarized


def train(
    backbone: str,
    train_dir: str,
    valid_dir: str,
    save_dir: str = "models/",
    batch_size: int = 64,
    binary: bool = False,
) -> nn.Module:
    run_name = get_run_name(backbone, binary)
    model_save_path = Path(save_dir) / Path(run_name)
    model_save_path.mkdir(parents=True, exist_ok=True)

    use_gpu = torch.cuda.is_available()
    device = torch.device("cuda" if use_gpu else "cpu")
    if use_gpu:
        torch.set_float32_matmul_precision("high")

    # generate augmentation
    image_processor = AutoImageProcessor.from_pretrained(backbone)
    img_size = (image_processor.size["height"], image_processor.size["width"])
    mean, std = image_processor.image_mean, image_processor.image_std
    train_aug = get_augmentation(img_size, mean, std)
    valid_aug = get_augmentation(img_size, mean, std, validation=True)

    # initialize datasets and dataloaders
    train_ds = HistoCRCDataset(
        train_dir, augmentation=train_aug, reduce_to_binary=binary
    )
    valid_ds = HistoCRCDataset(
        valid_dir, augmentation=valid_aug, reduce_to_binary=binary
    )
    train_dl = DataLoader(
        train_ds,
        batch_size=batch_size,
        shuffle=True,
        num_workers=os.cpu_count(),
        pin_memory=True,
        drop_last=True,
    )
    valid_dl = DataLoader(
        valid_ds,
        batch_size=batch_size,
        shuffle=False,
        num_workers=os.cpu_count(),
        pin_memory=True,
    )
    train_ds.describe()

    # initialize model
    model, config = load_model(backbone, train_ds.n_classes)
    model.to(device)
    print(model.head)
    print("Trainable parameters: ",
        sum(param.numel() for param in model.parameters() if param.requires_grad),
    )

    # initialize fastai learner
    weight = train_ds.inv_weights().to(device)
    criterion = nn.CrossEntropyLoss(weight=weight, label_smoothing=0)
    dls = DataLoaders(train_dl, valid_dl, device=device)
    learner = Learner(
        dls,
        model,
        loss_func=criterion,
        lr=1e-4,
        wd=1e-6,
        metrics=[
            AccumMetric(accuracy, flatten=False),
            Precision(average="macro"),
            Recall(average="macro"),
            F1Score(average="macro"),
            RocAucBinary(average="macro") if binary else RocAuc(average="macro"),
        ],
        path=model_save_path,
    )

    cbs = [
        # SaveModelCallback(monitor='valid_loss', fname=f'best_valid'),
        EarlyStoppingCallback(monitor="valid_loss", min_delta=0.01, patience=4),
        CSVLogger(),
    ]

    learner.fit_one_cycle(n_epoch=4, lr_max=1e-4, cbs=cbs)

    # save best checkpoint
    config.save_pretrained(model_save_path)
    torch.save(learner.model.state_dict(), model_save_path / "model.pt")

    # store performance of best checkpoint on validation dataset in file
    report = validate(model, valid_dl)
    print(report)
    with open(model_save_path / "report.txt", mode="w") as f:
        f.write(report)
    if not binary:
        report = validate_binarized(model, valid_dl)
        print(report)
        with open(model_save_path / "report.txt", mode="a") as f:
            f.write(5 * "\n" + "Binarized:")
            f.write(report)

    return model


def get_run_name(backbone: str, is_binary: bool) -> str:
    time = datetime.now().isoformat(timespec="milliseconds")

    backbone_name = backbone[backbone.find("/") + 1 :]
    run_name = f"{backbone_name}_binary={str(is_binary)}_{time}"
    return run_name


if __name__ == "__main__":
    backbone = "microsoft/swinv2-tiny-patch4-window8-256"  # "microsoft/swinv2-tiny-patch4-window8-256" #"google/efficientnet-b0" "google/efficientnet-b3"
    data_dir = Path("/home/aaron/Documents/Studium/Informatik/7_Semester/EKFZ/NewEPOC/data/")
    train_dir = data_dir / "NCT-CRC-HE-100K"
    valid_dir = data_dir / "CRC-VAL-HE-7K"
    save_dir = Path("/home/aaron/Documents/Studium/Informatik/7_Semester/EKFZ/NewEPOC/models/")

    model = train(backbone, train_dir, valid_dir, save_dir, binary=True, batch_size=32)