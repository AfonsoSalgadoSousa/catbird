from typing import Tuple

import ignite.distributed as idist
import torch
import torch.nn as nn
import torch.optim as optim
from catbird.core import Config  # type: ignore
from torch.cuda.amp import GradScaler, autocast
from torch.nn import CrossEntropyLoss

from .utils import JointEmbeddingLoss, freeze_params, one_hot


class GRUEncoder(nn.Module):
    def __init__(
        self,
        vocab_size,
        embedding_hidden_dims,
        embedding_out_dims,
        encoder_hidden_dims,
        encoder_out_dims,
        dropout_proba=0.5,
    ):
        super(GRUEncoder, self).__init__()
        self.vocab_size = vocab_size

        self.embedding_layer = nn.Sequential(
            nn.Linear(self.vocab_size, embedding_hidden_dims),
            nn.Threshold(0.000001, 0),
            nn.Linear(embedding_hidden_dims, embedding_out_dims),
            nn.Threshold(0.000001, 0),
        )
        self.gru_encoder = nn.GRU(embedding_out_dims, encoder_hidden_dims)
        self.linear = nn.Sequential(
            nn.Dropout(dropout_proba), nn.Linear(encoder_hidden_dims, encoder_out_dims),
        )

    def forward(self, input):
        """Forward function."""

        one_hot_input = one_hot(input, self.vocab_size)
        embedding_out = self.embedding_layer(one_hot_input)
        encoder_out = self.gru_encoder(embedding_out)[1]
        out = self.linear(encoder_out)

        return out


class EDD(nn.Module):
    """
    LSTM-based Encoder-Decoder-Discriminator (EDD) architecture.
    
    This is an implementation of paper `Learning Semantic Sentence Embeddings
    using Sequential Pair-wise Discriminator <https://aclanthology.org/C18-1230/>`.
    """

    def __init__(self, cfg):
        super(EDD, self).__init__()
        self.max_seq_len = cfg.data.max_length
        self.vocab_sz = cfg.embedding_length

        # encoder :
        self.emb_layer = nn.Sequential(
            nn.Linear(self.vocab_sz, cfg.model.emb_hid_dim),
            nn.Threshold(0.000001, 0),
            nn.Linear(cfg.model.emb_hid_dim, cfg.model.emb_dim),
            nn.Threshold(0.000001, 0),
        )
        self.enc_rnn = nn.GRU(cfg.model.emb_dim, cfg.model.enc_rnn_dim)
        self.enc_lin = nn.Sequential(
            nn.Dropout(cfg.model.enc_dropout),
            nn.Linear(cfg.model.enc_rnn_dim, cfg.model.enc_dim),
        )

        # generator :
        self.gen_emb = nn.Embedding(self.vocab_sz, cfg.model.emb_dim)
        self.gen_rnn = nn.LSTM(cfg.model.enc_dim, cfg.model.gen_rnn_dim)
        self.gen_lin = nn.Sequential(
            nn.Dropout(cfg.model.gen_dropout),
            nn.Linear(cfg.model.gen_rnn_dim, self.vocab_sz),
            nn.LogSoftmax(dim=-1),
        )

        # pair-wise discriminator :
        self.dis_emb_layer = nn.Sequential(
            nn.Linear(self.vocab_sz, cfg.model.emb_hid_dim),
            nn.Threshold(0.000001, 0),
            nn.Linear(cfg.model.emb_hid_dim, cfg.model.emb_dim),
            nn.Threshold(0.000001, 0),
        )
        self.dis_rnn = nn.GRU(cfg.model.emb_dim, cfg.model.enc_rnn_dim)
        self.dis_lin = nn.Sequential(
            nn.Dropout(cfg.model.enc_dropout),
            nn.Linear(cfg.model.enc_rnn_dim, cfg.model.enc_dim),
        )

    def forward(self, phrase, sim_phrase=None, train=False):
        """
        forward pass
        inputs :-
        phrase : given phrase , shape = (max sequence length, batch size)
        sim_phrase : (if train == True), shape = (max seq length, batch sz)
        train : if true teacher forcing is used to train the module
        outputs :-
        out : generated paraphrase, shape = (max sequence length, batch size, )
        enc_out : encoded generated paraphrase, shape=(batch size, enc_dim)
        enc_sim_phrase : encoded sim_phrase, shape=(batch size, enc_dim)
        """

        if sim_phrase is None:
            sim_phrase = phrase

        if train:
            # encode input phrase
            enc_phrase = self.enc_lin(
                self.enc_rnn(self.emb_layer(one_hot(phrase, self.vocab_sz)))[1]
            )

            # generate similar phrase using teacher forcing
            emb_sim_phrase_gen = self.gen_emb(sim_phrase)
            out_rnn, _ = self.gen_rnn(
                torch.cat([enc_phrase, emb_sim_phrase_gen[:-1, :]], dim=0)
            )
            out = self.gen_lin(out_rnn)

            # propagated from shared discriminator to calculate
            # pair-wise discriminator loss
            enc_sim_phrase = self.dis_lin(
                self.dis_rnn(self.dis_emb_layer(one_hot(sim_phrase, self.vocab_sz)))[1]
            )
            enc_out = self.dis_lin(self.dis_rnn(self.dis_emb_layer(torch.exp(out)))[1])

        else:
            # encode input phrase
            enc_phrase = self.enc_lin(
                self.enc_rnn(self.emb_layer(one_hot(phrase, self.vocab_sz)))[1]
            )

            # generate similar phrase using teacher forcing
            words = []
            h = None
            for _ in range(self.max_seq_len):
                word, h = self.gen_rnn(enc_phrase, hx=h)
                word = self.gen_lin(word)
                words.append(word)
                word = torch.multinomial(torch.exp(word[0]), 1)
                word = word.t()
                enc_phrase = self.gen_emb(word)
            out = torch.cat(words, dim=0)

            # propagated from shared discriminator to calculate
            # pair-wise discriminator loss
            enc_sim_phrase = self.dis_lin(
                self.dis_rnn(self.dis_emb_layer(one_hot(sim_phrase, self.vocab_sz)))[1]
            )
            enc_out = self.dis_lin(self.dis_rnn(self.dis_emb_layer(torch.exp(out)))[1])

        enc_out.squeeze_(0)
        enc_sim_phrase.squeeze_(0)
        return out, enc_out, enc_sim_phrase


def train_step(cfg, model, optimizer, device, scaler):
    def routine(engine, batch):
        if cfg.get("tokenizer", None):
            ignore_index = -100
        else:
            ignore_index = cfg.pad_token_id
        loss_fct = CrossEntropyLoss(ignore_index=ignore_index)

        model.train()

        if batch["tgt"].device != device:
            batch = {
                k: v.to(device, non_blocking=True, dtype=torch.long)
                for (k, v) in batch.items()
            }

        src_ids = batch["input_ids"]
        tgt = batch["tgt"]

        with autocast(enabled=cfg.train.with_amp):
            out, enc_out, enc_sim_phrase = model(src_ids, tgt)

            loss1 = loss_fct(out.permute(1, 2, 0), tgt)
            loss2 = JointEmbeddingLoss(enc_out, enc_sim_phrase)
            loss = loss1 + loss2

            loss /= cfg.train.accumulation_steps

        scaler.scale(loss).backward()

        if engine.state.iteration % cfg.train.accumulation_steps == 0:
            scaler.step(optimizer)
            scaler.update()
            optimizer.zero_grad()

        return {"batch loss": loss.item()}

    return routine


def initialize(cfg: Config) -> Tuple[nn.Module, optim.Optimizer]:
    """Initialize T5 conditional generator based on the given configurations.

    Args:
        cfg (Config): configuration file

    Returns:
        Tuple[nn.Module, optim.Optimizer]: model and optimizer
    """
    model = EDD(cfg)

    lr = cfg.train.learning_rate * idist.get_world_size()
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if not any(nd_entry in n for nd_entry in no_decay)
            ],
            "weight_decay": cfg.train.weight_decay,
        },
        {
            "params": [
                p
                for n, p in model.named_parameters()
                if any(nd_entry in n for nd_entry in no_decay)
            ],
            "weight_decay": 0.0,
        },
    ]
    if cfg.model.freeze_encoder:
        freeze_params(model)

    model = idist.auto_model(model)
    optimizer = optim.AdamW(optimizer_grouped_parameters, lr=lr)
    optimizer = idist.auto_optim(optimizer)

    return model, optimizer


if __name__ == "__main__":
    from catbird.core import Config
    from catbird.datasets import build_dataset, get_dataloaders
    from catbird.tokenizers import build_tokenizer

    cfg = Config.fromfile("configs/edl_quora.yaml")

    tokenizer = build_tokenizer(cfg)
    cfg.embedding_length = len(tokenizer)

    datasets = build_dataset(cfg, tokenizer, validate=False)
    dataloaders = get_dataloaders(cfg, *datasets)

    sample_batch = next(iter(dataloaders[0]))  # [train.batch_size, data.max_length]

    model = EDD(cfg)

    print(model(sample_batch["input_ids"], sample_batch["tgt"]))
