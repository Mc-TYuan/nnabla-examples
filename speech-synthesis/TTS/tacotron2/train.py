# Copyright (c) 2017 Sony Corporation. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from abc import ABC
from pathlib import Path

import matplotlib.pyplot as plt
from neu.tts.trainer import Trainer
import nnabla as nn
import nnabla.functions as F
import numpy as np
from scipy.io import wavfile
from tqdm import trange


def save_image(data, path, label, title, figsize=(6, 5)):
    r"""Saves an image to file."""
    plt.figure(figsize=figsize)
    plt.imshow(data.copy(), origin='lower', aspect='auto')
    plt.xlabel(label[0])
    plt.ylabel(label[1])
    plt.title(title)
    plt.colorbar()
    plt.savefig(path, bbox_inches='tight')
    plt.close()


class Tacotron2Trainer(Trainer):
    r"""Trainer for Tacotron2."""

    def update_graph(self, key='train'):
        r"""Builds the graph and update the placeholder.

        Args:
            key (str, optional): Type of computational graph. Defaults to 'train'.
        """
        assert key in ('train', 'valid')

        self.model.training = key != 'valid'
        hp = self.hparams

        # define input variables
        x_txt = nn.Variable([hp.batch_size, hp.text_len])
        x_mel = nn.Variable([hp.batch_size, hp.mel_len, hp.n_mels*hp.r])
        x_gat = nn.Variable([hp.batch_size, hp.mel_len])

        # output variables
        o_mel, o_mel_p, o_gat, o_att = self.model(x_txt, x_mel)
        o_mel = o_mel.apply(persistent=True)
        o_mel_p = o_mel_p.apply(persistent=True)
        o_gat = o_gat.apply(persistent=True)
        o_att = o_att.apply(persistent=True)

        # loss functions
        def criteria(x, t):
            return F.mean(F.squared_error(x, t))

        l_mel = (criteria(o_mel, x_mel) + criteria(o_mel_p, x_mel)).apply(persistent=True)
        l_gat = F.mean(F.sigmoid_cross_entropy(o_gat, x_gat)).apply(persistent=True)
        l_net = (l_mel + l_gat).apply(persistent=True)

        self.placeholder[key] = {
            'x_mel': x_mel, 'x_gat': x_gat, 'x_txt': x_txt,
            'o_mel': o_mel, 'o_mel_p': o_mel_p, 'o_gat': o_gat, 'o_att': o_att,
            'l_mel': l_mel, 'l_gat': l_gat, 'l_net': l_net
        }


    def train_on_batch(self):
        r"""Updates the model parameters."""
        batch_size = self.hparams.batch_size
        p, dl = self.placeholder['train'], self.dataloader['train']
        self.optimizer.zero_grad()
        if self.hparams.comm.n_procs > 1:
            self.hparams.event.default_stream_synchronize()
        p['x_mel'].d, p['x_txt'].d, p['x_gat'].d = dl.next()
        p['l_net'].forward(clear_no_need_grad=True)
        p['l_net'].backward(clear_buffer=True)
        self.monitor.update('train/l_mel', p['l_mel'].d.copy(), batch_size)
        self.monitor.update('train/l_gat', p['l_gat'].d.copy(), batch_size)
        self.monitor.update('train/l_net', p['l_net'].d.copy(), batch_size)
        if self.hparams.comm.n_procs > 1:
            self.hparams.comm.all_reduce(
                self._grads, division=True, inplace=False)
            self.hparams.event.add_default_stream_event()
        self.optimizer.update()

    def valid_on_batch(self):
        r"""Performs validation."""
        batch_size = self.hparams.batch_size
        p, dl = self.placeholder['valid'], self.dataloader['valid']
        if self.hparams.comm.n_procs > 1:
            self.hparams.event.default_stream_synchronize()
        p['x_mel'].d, p['x_txt'].d, p['x_gat'].d = dl.next()
        p['l_net'].forward(clear_buffer=True)
        self.loss.data += p['l_net'].d.copy() * batch_size
        self.monitor.update('valid/l_mel', p['l_mel'].d.copy(), batch_size)
        self.monitor.update('valid/l_gat', p['l_gat'].d.copy(), batch_size)
        self.monitor.update('valid/l_net', p['l_net'].d.copy(), batch_size)

    def callback_on_epoch_end(self):
        if self.hparams.comm.n_procs > 1:
            self.hparams.comm.all_reduce(
                [self.loss], division=True, inplace=False)
        self.loss.data /= self.dataloader['valid'].size
        if self.hparams.comm.rank == 0:
            p, hp = self.placeholder['train'], self.hparams
            self.monitor.info(f'valid/loss={self.loss.data[0]:.5f}\n')
            if self.cur_epoch % hp.epochs_per_checkpoint == 0:
                path = Path(hp.output_path) / 'output' / f'epoch_{self.cur_epoch}'
                path.mkdir(parents=True, exist_ok=True)
                # write attention and spectrogram outputs
                for k in ('o_att', 'o_mel'):
                    p[k].forward(clear_buffer=True)
                    data = p[k].d[0].copy()
                    save_image(
                        data=data.reshape(
                            (-1, hp.n_mels)).T if k == 'o_mel' else data.T,
                        path=path / (k + '.png'),
                        label=('Decoder timestep', 'Encoder timestep') if k == 'o_att' else (
                            'Frame', 'Channel'),
                        title={'o_att': 'Attention', 'o_mel': 'Mel spectrogram'}[k],
                        figsize=(6, 5) if k == 'o_att' else (6, 3)
                    )
                self.model.save_parameters(str(path / f'model_{self.cur_epoch}.h5'))
        self.loss.zero()
