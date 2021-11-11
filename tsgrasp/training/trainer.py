from omegaconf import DictConfig
import pytorch_lightning as pl
import os
from pytorch_lightning import loggers
from pytorch_lightning.callbacks import ModelCheckpoint,LearningRateMonitor
from pytorch_lightning.utilities import rank_zero_only

from hydra.utils import instantiate

class Trainer:
    def __init__(self, cfg : DictConfig):

        ## Lightning module and datamodule
        self.pl_model   = instantiate(cfg.model, training_cfg=cfg.training)
        self.pl_dataset = instantiate(cfg.data, batch_size=cfg.training.batch_size)

        ## Loggers
        tb_dir    = os.path.join(os.getcwd(), "tensorboard")
        os.makedirs(tb_dir, exist_ok=True)
        tb_logger = loggers.TensorBoardLogger(tb_dir)
        _loggers  = [tb_logger]

        self.add_wandb_logger(cfg, _loggers)
        
        self.pl_dataset.setup()
        example_batch = next(iter(self.pl_dataset.train_dataloader()))
        _callbacks = [
            ModelCheckpoint(every_n_epochs=1), 
            LearningRateMonitor(logging_interval='step'),
        ]

        if cfg.training.save_animations:
            from tsgrasp.utils.viz.viz import GraspAnimationLogger
            _callbacks.append(GraspAnimationLogger(example_batch))

        ## Lightning Trainer
        if "resume_from_checkpoint" in cfg.training:
            ckpt = cfg.training.resume_from_checkpoint
        else:
            ckpt = None

        kwargs = dict(strategy="ddp") if cfg.training.gpus > 1 else {}

        # if True:
        #     kwargs.update(dict(overfit_batches=1, check_val_every_n_epoch=100))

        self.trainer = pl.Trainer(
            gpus=cfg.training.gpus,
            logger=_loggers,
            log_every_n_steps=10,
            callbacks=_callbacks,
            max_epochs=cfg.training.max_epochs,
            resume_from_checkpoint=ckpt,
            **kwargs
        )

    @rank_zero_only
    def add_wandb_logger(self, cfg, _loggers):
        if cfg.training.use_wandb:
            import wandb
            wandb.init(group="DDP")
            wandb_logger = loggers.WandbLogger(
                project=cfg.training.wandb.project, 
                log_model="all", 
                name=cfg.training.wandb.experiment
            )
            wandb_logger.watch(self.pl_model)
            _loggers.append(wandb_logger)

    def train(self):
        self.trainer.fit(self.pl_model, datamodule=self.pl_dataset)

    def test(self):
        self.trainer.test(self.pl_model, datamodule=self.pl_dataset)