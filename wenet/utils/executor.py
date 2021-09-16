# Copyright 2019 Mobvoi Inc. All Rights Reserved.
# Author: binbinzhang@mobvoi.com (Binbin Zhang)

import logging
from contextlib import nullcontext
# if your python version < 3.7 use the below one
# from contextlib import suppress as nullcontext
import torch
from torch.nn.utils import clip_grad_norm_


class Executor:
    def __init__(self, models, criterion, device, args):
        self.step = 0
        self.models = models
        self.criterion = criterion
        self.device = device
        self.clip = args.get('grad_clip', 50.0)
        self.log_interval = args.get('log_interval', 10)
        self.rank = args.get('rank', 0)
        self.accum_grad = args.get('accum_grad', 1)
        self.is_distributed = args.get('is_distributed', True)
        self.use_amp = args.get('use_amp', False)

    def train(self, optimizer, scheduler, data_loader, writer, scaler):
        ''' Train one epoch
        '''
        self.models[0].train()
        logging.info('using accumulate grad, new batch size is {} times'
                     'larger than before'.format(self.accum_grad))
        if self.use_amp:
            assert scaler is not None
        num_seen_utts = 0
        num_total_batch = len(data_loader)
        for batch_idx, batch in enumerate(data_loader):
            key, feats, target, feats_lengths, target_lengths = batch
            feats = feats.to(self.device)
            target = target.to(self.device)
            feats_lengths = feats_lengths.to(self.device)
            target_lengths = target_lengths.to(self.device)
            num_utts = target_lengths.size(0)
            if num_utts == 0:
                continue
            context = None
            # Disable gradient synchronizations across DDP processes.
            # Within this context, gradients will be accumulated on module
            # variables, which will later be synchronized.
            if self.is_distributed and batch_idx % self.accum_grad != 0:
                context = self.models[0].no_sync
            # Used for single gpu training and DDP gradient synchronization
            # processes.
            else:
                context = nullcontext
            with context():
                # autocast context
                # The more details about amp can be found in
                # https://pytorch.org/docs/stable/notes/amp_examples.html
                with torch.cuda.amp.autocast(scaler is not None):
                    outputs = []
                    for model in self.models:
                        outputs.append(model(feats, feats_lengths, target, target_lengths))
                    losses = self.criterion.get_losses(outputs, target, target_lengths)
                    loss = losses['loss'] / self.accum_grad
                if self.use_amp:
                    scaler.scale(loss).backward()
                else:
                    loss.backward()

            num_seen_utts += num_utts
            if batch_idx % self.accum_grad == 0:
                if self.rank == 0 and writer is not None:
                    writer.add_scalar('train_loss', loss, self.step)
                # Use mixed precision training
                if self.use_amp:
                    scaler.unscale_(optimizer)
                    grad_norm = clip_grad_norm_(self.models[0].parameters(), self.clip)
                    # Must invoke scaler.update() if unscale_() is used in the
                    # iteration to avoid the following error:
                    #   RuntimeError: unscale_() has already been called
                    #   on this optimizer since the last update().
                    # We don't check grad here since that if the gradient has
                    # inf/nan values, scaler.step will skip optimizer.step().
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    grad_norm = clip_grad_norm_(self.models[0].parameters(), self.clip)
                    if torch.isfinite(grad_norm):
                        optimizer.step()
                optimizer.zero_grad()
                scheduler.step()
                self.step += 1
            if batch_idx % self.log_interval == 0:
                lr = optimizer.param_groups[0]['lr']
                log_str = 'TRAIN Batch {}/{} loss {:.6f} '.format(
                    batch_idx, num_total_batch,
                    loss.item() * self.accum_grad)
                for key, value in losses.items():
                    if key == 'loss': continue
                    log_str += '{} {:.6f} '.format(key, value.item())
                log_str += 'lr {:.8f} rank {}'.format(lr, self.rank)
                logging.debug(log_str)

    def cv(self, data_loader):
        ''' Cross validation on
        '''
        self.models[0].eval()
        # in order to avoid division by 0
        num_seen_utts = 1
        total_loss = 0.0
        num_total_batch = len(data_loader)
        with torch.no_grad():
            for batch_idx, batch in enumerate(data_loader):
                key, feats, target, feats_lengths, target_lengths = batch
                feats = feats.to(self.device)
                target = target.to(self.device)
                feats_lengths = feats_lengths.to(self.device)
                target_lengths = target_lengths.to(self.device)
                num_utts = target_lengths.size(0)
                if num_utts == 0:
                    continue
                outputs = self.models[0](feats, feats_lengths, target, target_lengths)
                losses = self.criterion.get_losses(outputs, target, target_lengths)
                loss = losses['loss']
                if torch.isfinite(loss):
                    num_seen_utts += num_utts
                    total_loss += loss.item() * num_utts
                if batch_idx % self.log_interval == 0:
                    log_str = 'CV Batch {}/{} loss {:.6f} '.format(
                        batch_idx, num_total_batch, loss.item())
                    for key, value in losses.items():
                        if key == 'loss': continue
                        log_str += '{} {:.6f} '.format(key, value.item())
                    log_str += 'history loss {:.6f}'.format(total_loss /
                                                            num_seen_utts)
                    logging.debug(log_str)

        return total_loss, num_seen_utts
