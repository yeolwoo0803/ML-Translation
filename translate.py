import argparse
import sys
import codecs
from operator import itemgetter

import torch

from nmt.data_loader import DataLoader
import nmt.data_loader as data_loader
from nmt.models.seq2seq import Seq2Seq

def define_argparser():
    p = argparse.ArgumentParser()

    p.add_argument(
        '--model_fn',
        required=True,
        help='Model file name to use'
    )
    p.add_argument(
        '--gpu_id',
        type=int,
        default=-1,
        help='GPU ID to use. -1 for CPU. Default=%(default)s'
    )

    p.add_argument(
        '--batch_size',
        type=int,
        default=128,
        help='Mini batch size for parallel inference. Default=%(default)s'
    )
    p.add_argument(
        '--max_length',
        type=int,
        default=255,
        help='Maximum sequence length for inference. Default=%(default)s'
    )
    p.add_argument(
        '--n_best',
        type=int,
        default=1,
        help='Number of best inference result per sample. Default=%(default)s'
    )
    p.add_argument(
        '--beam_size',
        type=int,
        default=5,
        help='Beam size for beam search. Default=%(default)s'
    )
    p.add_argument(
        '--lang',
        type=str,
        default=None,
        help='Source language and target language. Example: enko'
    )
    p.add_argument(
        '--length_penalty',
        type=float,
        default=1.2,
        help='Length penalty parameter that higher value produce shorter results. Default=%(default)s',
    )

    config = p.parse_args()

    return config

def read_text(batch_size = 128):
    lines = []

    sys.stdin = codecs.getreader('utf-8')(sys.stdin.detach())

    for line in sys.stdin:
        if line.strip() != '':
            lines += [line.strip().split(' ')]

        if len(lines) >= batch_size:
            yield lines
            lines = []

    if len(lines) > 0:
        yield lines

def to_text(indice, vocab):
    lines = []

    for i in range(len(indice)):
        line = []
        for j in range(len(indice[i])):
            index = indice[i][j]
            if index == data_loader.EOS:
                break

            else:
                line += [vocab.itos[index]]

        line = ' '.join(line)
        lines += [line]

    return lines

def is_dsl(train_config):
    return not ('rl_n_epochs' in vars(train_config).keys())


def get_vocabs(train_config, config, saved_data):
    if is_dsl(train_config):
        assert config.lang is not None

        if config.lang == train_config.lang:
            is_reverse = False
        
        else:
            is_reverse = True

        if not is_reverse:
            src_vocab = saved_data['src_vocab']
            tgt_vocab = saved_data['tgt_vocab']

        else:
            src_vocab = saved_data['tgt_vocab']
            tgt_vocab = saved_data['src_vocab']

        return src_vocab, tgt_vocab, is_reverse
    
    else:
        src_vocab = saved_data['src_vocab']
        tgt_vocab = saved_data['tgt_vocab']
        
    return src_vocab, tgt_vocab, False


def get_model(input_size, output_size, train_config, is_reverse, saved_data):
    if 'use_transformer' in vars(train_config).keys() and train_config.use_transformer:
        pass

    else:
        model = Seq2Seq(
            input_size,
            train_config.word_vec_size,
            train_config.hidden_size,
            output_size,
            n_layers = train_config.n_layers,
            dropout_p=train_config.dropout,
        )

    if is_dsl(train_config):
        pass

    else:
        model.load_state_dict(saved_data['model'])
    model.eval()

    return model

 
if __name__=='__main__':
    sys.stdout = codecs.getwriter('utf-8')(sys.stdout.detach())
    config = define_argparser()

    saved_data = torch.load(
        config.model_fn,
        map_location='cpu' if config.gpu_id < 0 else 'cuda:%d'%config.gpu_id
    )

    train_config = saved_data['config']

    src_vocab, tgt_vocab, is_reverse = get_vocabs(train_config, config, saved_data)

    loader = DataLoader()
    loader.load_vocab(src_vocab, tgt_vocab)

    input_size, output_size = len(loader.src.vocab), len(loader.tgt.vocab)
    model = get_model(input_size, output_size, train_config, is_reverse, saved_data)

    if config.gpu_id >= 0:
        model.cuda(config.gpu_id)

    with torch.no_grad():
        for lines in read_text(batch_size=config.batch_size):
            
            lengths = [len(line) for line in lines]
            original_indice = [i for i in range(config.batch_size)]
            # print(lines)
            sorted_tuples = sorted(
                zip(lines, lengths, original_indice),
                key = itemgetter(1),
                reverse=True
            )

            sorted_lines = [sorted_tuples[i][0] for i in range(len(sorted_tuples))]
            lengths = [sorted_tuples[i][1] for i in range(len(sorted_tuples))]
            original_indice = [sorted_tuples[i][2] for i in range(len(sorted_tuples))]

            # ONE-HOT VECTOR
            x = loader.src.numericalize(
                loader.src.pad(sorted_lines), # pad 씌움.
                device = 'cuda:%d' % config.gpu_id if config.gpu_id >= 0 else 'cpu'
            )
            # |x| = (batch_size, length, )
            # print(x)
            if config.beam_size == 1:
                y_hat, indice = model.search(x)
                output = to_text(indice, loader.tgt.vocab)

                sorted_tuples = sorted(zip(output, original_indice), key = itemgetter(1))
                output = [sorted_tuples[i][0] for i in range(len(sorted_tuples))]

                sys.stdout.write('\n'.join(output) + '\n')

            else:
                pass

