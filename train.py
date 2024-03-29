import math
import os
import time
import yaml

import logging
import os.path as osp

import hydra
from omegaconf import DictConfig, OmegaConf

from vizier.service import clients
from vizier.service import pyvizier as vz

import torch
import torch.nn as nn
from torch.utils.tensorboard import SummaryWriter

from cprnn.utils import load_object, AverageMeter
from cprnn.models import CPRNN, SecondOrderRNN, LSTMPT, MRNN, MIRNN
from cprnn.features.ptb_dataloader import PTBDataloader
from cprnn.features.tokenizer import CharacterTokenizer

_output_paths = {
    "models": "models"
}

_models = {
    "cprnn": CPRNN,
    "2rnn": SecondOrderRNN,
    "lstmpt": LSTMPT,
    "mrnn": MRNN,
    "mirnn": MIRNN
}


def hpopt(hps, hp_ranges, hp_types, evaluate_fn, evaluate_kwargs, iters=10, metric_name="bpc"):
    # Algorithm, search space, and metrics.
    study_config = vz.StudyConfig(algorithm='GAUSSIAN_PROCESS_BANDIT')

    add_param = {
        "int": study_config.search_space.root.add_int_param,
        "float": study_config.search_space.root.add_float_param,
        "discrete": study_config.search_space.root.add_discrete_param
    }

    for hp, hp_range, hp_type in zip(hps, hp_ranges, hp_types):
        study_config.search_space.root.add_float_param('w', 0.0, 5.0)
        add_param[hp_type](hp, *hp_range)

    study_config.metric_information.append(vz.MetricInformation('metric_name', goal=vz.ObjectiveMetricGoal.MINIMIZE))

    # Setup client and begin optimization. Vizier Service will be implicitly created.
    study = clients.Study.from_study_config(study_config, owner='my_name', study_id='example')
    for i in range(iters):
        suggestions = study.suggest(count=1)
        for suggestion in suggestions:
            params = suggestion.parameters
            # objective = evaluate_fn(params['w'], params['x'], params['y'], params['z'], **evaluate_kwargs)
            objective = evaluate_fn(**params, **evaluate_kwargs)
            suggestion.complete(vz.Measurement({metric_name: objective}))


def get_experiment_name(configs, abbrevs=None):

    if abbrevs is None:
        abbrevs = {}

    for key, value in configs.items():
        if isinstance(value, dict):
            get_experiment_name(value, abbrevs)
        else:
            i = 1
            while i < len(key):
                if key[:i] not in abbrevs:
                    abbrevs[key[:i]] = value
                    break
                i += 1

    return abbrevs


@hydra.main(version_base=None, config_path="./", config_name="configs")
def main(cfg: DictConfig):

    args = OmegaConf.to_container(cfg, resolve=True)

    if (args['data']['tokenizer'] == 'word') ^ (args['model']['input_size'] != 0):
        raise ValueError("Embedding dimension and word tokenizer must be set jointly")

    for t in range(args["runs"]):
        exp_name = get_experiment_name(
            {**args["train"], **args['model'], **{"tokenizer": args['data']['tokenizer'], "trial": t}}
        )
        folder_name = "_".join(["{}{}".format(k, v) for k, v in exp_name.items()])
        dct_latest, dct_best = None, None

        output_path = osp.join(args['data']['output'], osp.split(args["data"]["path"])[-1], folder_name)
        if not osp.exists(output_path):
            os.makedirs(output_path)
            print("Running Experiment: `{}`".format(folder_name))

        elif not osp.exists(osp.join(output_path, 'model_latest.pth')):
            print("Running Experiment: `{}`".format(folder_name))

        else:  # Experiment already exists
            dct_latest = torch.load(osp.join(output_path, 'model_latest.pth'))
            dct_best = torch.load(osp.join(output_path, 'model_best.pth'))
            if dct_latest['epoch'] >= args['train']['epochs']:
                print("Experiment `{}` already exists. (Latest @ epoch {})".format(
                    folder_name, dct_latest['epoch']
                ))
                continue

            else:
                print("Running Experiment: `{}`".format(folder_name))

        with open(osp.join(output_path, 'configs.yaml'), 'w') as outfile:
            yaml.dump(args, outfile)

        writer = SummaryWriter(log_dir=output_path)

        # Logging configuration
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
            datefmt='%H:%M:%S',
            filemode='a'
        )
        logging.getLogger().addHandler(logging.FileHandler(osp.join(output_path, "logging.txt")))

        # Data
        train_dataloader = PTBDataloader(
            osp.join(args["data"]["path"], 'train-{}.pth'.format(args['data']['tokenizer'])), batch_size=args["train"]["batch_size"],
            seq_len=args["train"]["seq_len"]
        )
        valid_dataloader = PTBDataloader(
            osp.join(args["data"]["path"], 'valid-{}.pth'.format(
                args['data']['tokenizer'])), batch_size=args["train"]["batch_size"], seq_len=args["train"]["seq_len"]
        )
        test_dataloader = PTBDataloader(
            osp.join(args["data"]["path"], 'test-{}.pth'.format(
                args['data']['tokenizer'])), batch_size=args["train"]["batch_size"], seq_len=args["train"]["seq_len"]
        )
        tokenizer = CharacterTokenizer(
            tokens=load_object(osp.join(args['data']['path'], 'tokenizer-{}.pkl'.format(args['data']['tokenizer'])))
        )

        device = torch.device('cuda:0') if torch.cuda.is_available() else torch.device('cpu')
        logging.info("Device: {}".format(device))

        # Model
        model = _models[args["model"]["name"].lower()](vocab_size=tokenizer.vocab_size, **args["model"])
        num_params = sum(p.numel() for p in model.parameters() if p.requires_grad)

        criterion = nn.CrossEntropyLoss()

        if dct_latest is not None:
            model.load_state_dict(dct_latest['model_state_dict'])
            model.to(device)
            optimizer = torch.optim.Adam(model.parameters(), lr=args["train"]["lr"])
            optimizer.load_state_dict(dct_latest['optimizer_state_dict'])
            curr_epoch = dct_latest['epoch']
            curr_best_valid_loss = dct_best['valid_metrics']['loss']
            print("Resuming training from from epoch {}".format(dct_latest['epoch']))

        else:
            curr_epoch = 0
            curr_best_valid_loss = None
            model.to(device)
            optimizer = torch.optim.Adam(model.parameters(), lr=args["train"]["lr"])

        # Parallelize the model
        if torch.cuda.device_count() > 1:
            print("Using {} GPUs".format(torch.cuda.device_count()))
            model = nn.DataParallel(model)

        # Training
        train(
            model, args, criterion, optimizer, train_dataloader, valid_dataloader, test_dataloader, device, num_params,
            output_path, tokenizer, writer, curr_epoch=curr_epoch+1, best_valid_loss=curr_best_valid_loss
        )

        print("Experiment: `{}` Succeeded".format(folder_name))


def train(model, args, criterion, optimizer, train_dataloader, valid_dataloader, test_dataloader, device, num_params,
          output_path, tokenizer, writer, curr_epoch=1, best_valid_loss=None):

    for i_epoch in range(curr_epoch, args["train"]["epochs"] + 1):
        epoch_start_time = time.time()
        train_metrics = train_epoch(
            model, train_dataloader, optimizer, criterion, clip=args["train"]["grad_clip"], device=device
        )
        valid_metrics = evaluate(model, valid_dataloader, criterion, device=device)

        logging.info(
            'Epoch {:4d}/{:4d} | time: {:5.2f}s | train loss {:5.2f} | train ppl {:8.2f} | train bpc {:8.2f} | '
            'valid loss {:5.2f} | valid ppl {:8.2f} | valid bpc {:8.2f}'.format(
                i_epoch, args["train"]["epochs"], (time.time() - epoch_start_time),
                train_metrics['loss'], train_metrics['ppl'], train_metrics['bpc'],
                valid_metrics['loss'], valid_metrics['ppl'], valid_metrics['bpc']
            ))

        # Save the model if the validation loss is the best we've seen so far.
        if best_valid_loss is None or valid_metrics['loss'] < best_valid_loss:
            # Compute here for convenience
            test_metrics = evaluate(model, test_dataloader, criterion, device=device)

            torch.save({
                'epoch': i_epoch,
                'optimizer_state_dict': optimizer.state_dict(),
                'model_state_dict': model.state_dict(),
                'torchrandom_state': torch.get_rng_state(),
                'train_metrics': valid_metrics,
                'valid_metrics': valid_metrics,
                'test_metrics': test_metrics,
                'num_params': num_params,
                'config': args
            }, osp.join(output_path, "model_best.pth"))

            best_valid_loss = valid_metrics['loss']

            # Save the latest model
            torch.save({
                'epoch': i_epoch,
                'optimizer_state_dict': optimizer.state_dict(),
                'model_state_dict': model.state_dict(),
                'torchrandom_state': torch.get_rng_state(),
                'train_metrics': valid_metrics,
                'valid_metrics': valid_metrics,
                'test_metrics': test_metrics,
                'num_params': num_params,
                'config': args
            }, osp.join(output_path, "model_latest.pth"))

        elif i_epoch % 5 == 0 or i_epoch == args["train"]["epochs"]:
            test_metrics = evaluate(model, test_dataloader, criterion, device=device)
            torch.save({
                'epoch': i_epoch,
                'optimizer_state_dict': optimizer.state_dict(),
                'model_state_dict': model.state_dict(),
                'torchrandom_state': torch.get_rng_state(),
                'train_metrics': valid_metrics,
                'valid_metrics': valid_metrics,
                'test_metrics': test_metrics,
                'num_params': num_params,
                'config': args
            }, osp.join(output_path, "model_latest.pth"))

        # Qualitative prediction
        train_sent_output, train_sent_target, train_sent_source = evaluate_qualitative(
            model, train_dataloader, tokenizer, device,
        )
        valid_sent_output, valid_sent_target, valid_sent_source = evaluate_qualitative(
            model, valid_dataloader, tokenizer, device,
        )

        valid_sent_output = valid_sent_output.transpose(1, 0)
        valid_sent_target = valid_sent_target.transpose(1, 0)
        valid_sent_source = valid_sent_source.transpose(1, 0)
        train_sent_output = train_sent_output.transpose(1, 0)
        train_sent_target = train_sent_target.transpose(1, 0)
        train_sent_source = train_sent_source.transpose(1, 0)

        valid_qaul_str = "Source:  \n{}  \nTarget:  \n{}  \nPrediction:  \n{}".format(
            "".join(valid_sent_source[:, 0]), "".join(valid_sent_target[:, 0]), "".join(valid_sent_output[:, 0])
        )

        train_qaul_str = "Source:  \n{}  \nTarget:  \n{}  \nPrediction:  \n{}".format(
            "".join(train_sent_source[:, 0]), "".join(train_sent_target[:, 0]), "".join(train_sent_output[:, 0])
        )

        sample_str = sample(model, size=100, prime='The', top_k=5, device=device, tokenizer=tokenizer)

        # Sample
        logging.info("Train:\n{}".format(train_qaul_str))
        logging.info("Validation:\n{}".format(valid_qaul_str))
        logging.info("Sample:\n{}".format(sample_str))

        # Logging
        for m in train_metrics.keys():
            writer.add_scalar("train/{}".format(m), train_metrics[m], i_epoch)
            writer.add_scalar("valid/{}".format(m), valid_metrics[m], i_epoch)

        writer.add_scalar("LR", args["train"]["lr"], i_epoch)
        writer.add_text('Valid', valid_qaul_str, i_epoch)
        writer.add_text('Train', train_qaul_str, i_epoch)
        writer.add_text('Sample', sample_str, i_epoch)

    writer.flush()
    writer.close()

    return valid_metrics


def sample(model, tokenizer, device=torch.device('cpu'), size=100, prime='The', top_k=5):
    # First off, run through the prime characters
    chars = [ch for ch in prime]

    model_alias = model.module if isinstance(model, nn.DataParallel) else model
    init_states = model_alias.init_hidden(batch_size=1, device=device)

    for ch in prime:
        inp = torch.tensor(tokenizer.char_to_ix(ch)).reshape(1, 1).to(device)
        output_id, init_states = model_alias.predict(inp, init_states, top_k=top_k, device=device)

    chars.append(tokenizer.ix_to_char(output_id.item()))

    # Now pass in the previous character and get a new one
    for ii in range(size):
        inp = torch.tensor(tokenizer.char_to_ix(chars[-1])).reshape(1, 1).to(device)
        output_id, init_states = model_alias.predict(inp, init_states, top_k=top_k, device=device)
        chars.append(tokenizer.ix_to_char(output_id.item()))

    return ''.join(chars)


def evaluate_qualitative(model, eval_dataloader, tokenizer: CharacterTokenizer, device: torch.device):
    with torch.no_grad():
        source, target = next(iter(eval_dataloader))
        source, target = source.to(device), target.to(device)
        output, _ = model(source)  # [bsz, seq, d_vocab]
        output = torch.argmax(torch.softmax(output, dim=-1), dim=-1)
        sentences_output = tokenizer.ix_to_char(output.cpu().detach().numpy())
        sentences_target = tokenizer.ix_to_char(target.cpu().detach().numpy())
        sentences_source = tokenizer.ix_to_char(source.cpu().detach().numpy())
    return sentences_output, sentences_target, sentences_source


def evaluate(model, eval_dataloader, criterion, device):
    with torch.no_grad():
        loss_average_meter = AverageMeter()
        ppl_average_meter = AverageMeter()
        for inputs, targets in eval_dataloader:
            inputs, targets = inputs.to(device), targets.to(device)

            output, _ = model(inputs)
            n_seqs_curr, n_steps_curr = output.shape[0], output.shape[1]
            loss = criterion(output.reshape(n_seqs_curr * n_steps_curr, -1),
                             targets.reshape(n_seqs_curr * n_steps_curr))

            loss_average_meter.add(loss.item())
            ppl_average_meter.add(torch.exp(loss).item())

    return {"loss": loss_average_meter.value,
            "ppl": ppl_average_meter.value,
            "bpc": loss_average_meter.value / math.log(2)}


def train_epoch(model, train_dataloader, optimizer, criterion, clip=5, device=torch.device('cpu')):
    model.train()
    loss_average_meter = AverageMeter()
    ppl_average_meter = AverageMeter()

    # for x, y in get_batches(data, n_seqs, n_steps):
    for i_batch, (inputs, targets) in enumerate(train_dataloader):  # [L, BS]

        inputs, targets = inputs.to(device), targets.to(device)

        model.zero_grad()
        output, _ = model.forward(inputs)

        n_seqs_curr, n_steps_curr = output.shape[0], output.shape[1]
        loss = criterion(output.reshape(n_seqs_curr * n_steps_curr, -1),
                         targets.reshape(n_seqs_curr * n_steps_curr))
        loss.backward()

        # `clip_grad_norm` helps prevent the exploding gradient problem in RNNs / LSTMs.
        if clip != 'inf':
            nn.utils.clip_grad_norm_(model.parameters(), clip)

        optimizer.step()

        loss_average_meter.add(loss.item())
        ppl_average_meter.add(torch.exp(loss).item())

    return {"loss": loss_average_meter.value,
            "ppl": ppl_average_meter.value,
            "bpc": loss_average_meter.value / math.log(2)}


if __name__ == '__main__':
    main()

    """
    Commands
    
    // Semantic: toy rnn dataset generated with certain input size, hidden size vocab size rank
    python train.py -d data/processed/toy-2rnnkr-i32-h32-v16-r32
    
    python train.py -d data/processed/anna
    
    """
