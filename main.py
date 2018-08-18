import argparse
import pickle
import time

import torch
import torch.nn as nn
import torch.utils.data as data
from tqdm import tqdm

import model
import utils
from utils import PadCollate, RunningAverage, TextDataset, TextSampler
from pruner import ModelPruner


parser = argparse.ArgumentParser(description='PyTorch IMDB LSTM classifier')

parser.add_argument('--load', action='store_true',
                    help='Load dataset from disk')
parser.add_argument('--emsize', type=int, default=300,
                    help='size of word embeddings')
parser.add_argument('--nhid', type=int, default=128,
                    help='number of hidden units per layer')
parser.add_argument('--lr', type=float, default=1e-3,
                    help='initial learning rate')
parser.add_argument('--clip', type=float, default=0.25,
                    help='gradient clipping')
parser.add_argument('--epochs', type=int, default=5,
                    help='upper epoch limit')
parser.add_argument('--batch_size', type=int, default=64, metavar='N',
                    help='batch size')
parser.add_argument('--seed', type=int, default=42,
                    help='random seed for reprodusability')
parser.add_argument('--bptt', type=int, default=70,
                    help='sequence length')
parser.add_argument('--save', type=str, default='data/models/model.pt',
                    help='path to save the final model')
parser.add_argument('--collectq', action='store_true',
                    help='output weights 90 percentile for pruning')
parser.add_argument('--prune', action='store_true',
                    help='use pruning while training')
parser.add_argument('--config', type=str, default='data/config.yaml',
                    help='model configuration file')

args = parser.parse_args()
torch.manual_seed(args.seed)
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

###############################################################################
# Load data
###############################################################################


train_ds = TextDataset(load=args.load, train=True)
test_ds = TextDataset(load=True, train=False)


train_sp = TextSampler(train_ds, key=lambda i: len(train_ds.texts[i]), batch_size=args.batch_size)
train_dl = data.DataLoader(train_ds, batch_size=args.batch_size, sampler=train_sp, collate_fn=PadCollate())
test_dl = data.DataLoader(test_ds, batch_size=args.batch_size, shuffle=True, collate_fn=PadCollate())

with open('data/dataset/itos.pkl', 'rb') as f:
    itos = pickle.load(f)

print('Loaded train and test data.')


###############################################################################
# Create model
###############################################################################


ntokens = len(itos)
md = nn.Sequential(
    model.ClassifierRNN(args.bptt, ntokens, args.emsize, args.nhid),
    model.LinearDecoder(args.nhid, 1)
).to(device)

if args.prune:
    config = utils.parse_config(args.config)
    pruner = ModelPruner(md, config)

print(f'Created model with {utils.count_parameters(md)} parameters:')
print(md)

criterion = nn.BCEWithLogitsLoss(reduction='sum')
optimizer = torch.optim.Adam(md.parameters(), lr=args.lr, betas=(0.8, 0.99))

###############################################################################
# Training code
###############################################################################


def evaluate():
    """Calculates loss and prediction accuracy given torch dataloader"""
    # Turn on evaluation mode which disables dropout.
    md.eval()
    avg_loss = RunningAverage()
    avg_acc = RunningAverage()

    with torch.no_grad():
        pbar = tqdm(test_dl, ascii=True, leave=False)
        for batch in pbar:
            # run model
            inp, target = batch
            inp, target = inp.to(device), target.to(device)
            out = md(inp.t())

            # calculate loss
            loss = criterion(out.view(-1), target.float())
            avg_loss.update(loss.item())

            # calculate accuracy
            pred = out.view(-1) > 0.5
            correct = pred == target.byte()
            avg_acc.update(torch.sum(correct).item() / len(correct))

            pbar.set_postfix(loss=f'{avg_loss():05.3f}', acc=f'{avg_acc():05.2f}')

    return avg_loss(), avg_acc()


def train():
    # Turn on training mode which enables dropout.
    md.train()
    avg_loss = RunningAverage()
    avg_acc = RunningAverage()

    pbar = tqdm(train_dl, ascii=True, leave=False)
    for batch in pbar:
        inp, target = batch
        inp, target = inp.to(device), target.to(device)
        # run model
        md.zero_grad()
        out = md(inp.t())
        loss = criterion(out.view(-1), target.float())
        loss.backward()

        torch.nn.utils.clip_grad_norm_(md.parameters(), args.clip)
        optimizer.step()
        if args.prune:
            pruner.step()

        # upgrade stats
        avg_loss.update(loss.item())
        pred = out.view(-1) > 0.5
        correct = pred == target.byte()
        avg_acc.update(torch.sum(correct).item() / len(correct))

        pbar.set_postfix(loss=f'{avg_loss():05.3f}', acc=f'{avg_acc():05.2f}')

    return avg_loss(), avg_acc()


###############################################################################
# Actual training
###############################################################################

# Loop over epochs.
lr = args.lr
best_val_loss = None


for epoch in range(1, args.epochs+1):
    epoch_start_time = time.time()
    trn_loss, trn_acc = train()
    val_loss, val_acc = evaluate()
    print('-' * 100)
    print(f'| end of epoch {epoch:3d} | time: {time.time()-epoch_start_time:5.2f}s '
          f'| train/valid loss {trn_loss:05.3f}/{val_loss:05.3f} | train/valid acc {trn_acc:04.2f}/{val_acc:04.2f}')
    print('-' * 100)
    # Save the model if the validation loss is the best we've seen so far.
    if not best_val_loss or val_loss < best_val_loss:
        with open(args.save, 'wb') as f:
            torch.save(md, f)
        best_val_loss = val_loss
    else:
        # Anneal the learning rate if no improvement has been seen in the validation dataset.
        lr /= 4.0


if args.collectq:
    with open('q_value', 'w') as fd:
        for name, param in md.named_parameters():
            sorted_weights, _ = torch.sort(param.view(-1), descending=True)
            q = abs(sorted_weights[int(sorted_weights.numel() * 0.9)].item())
            fd.write(f'{name:20}|  q={q}\n')
