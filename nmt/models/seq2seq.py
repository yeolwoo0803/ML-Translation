import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as unpack

import nmt.data_loader as data_loader

class Attention(nn.Module):
    
    def __init__(self, hidden_size):
        super(Attention, self).__init__()

        self.linear = nn.Linear(hidden_size, hidden_size, bias = False)
        self.softmax = nn.Softmax(dim = -1)

    def forward(self, h_src, h_t_tgt, mask = None):

        query = self.linear(h_t_tgt)
        # |query} = (batch_size, 1, hidden_size)
        
        weight = torch.bmm(query, h_src.transpose(1, 2))
        # |h_src| = (batch_size, length, hidden_size)
        # |weight| = (batch_size, 1, length)

        if mask is not None:
            weight.masked_fill_(mask.unsqueeze(1), -float('inf'))

        weight = self.softmax(weight)
        # |weight| = (batch_size, 1, length)
        context_vector = torch.bmm(weight, h_src)
        # |context_vector| = (batch_size, 1, length) * (batch_size, length, hidden_size)

        return context_vector


class Encoder(nn.Module):

    def __init__(self, word_vec_dim, hidden_size, n_layers, dropout_p):
        super(Encoder, self).__init__()

        self.rnn = nn.LSTM(
            word_vec_dim,
            int(hidden_size / 2),
            num_layers = n_layers,
            dropout = dropout_p,
            bidirectional = True,
            batch_first = True,
        )
    
    def forward(self, emb):
        # |emb| = (batch_size, length, word_vec_dim)

        if isinstance(emb, tuple):
            x, lengths = emb
            x = pack(x, lengths.tolist(), batch_first = True)
        
        # a = [torch.tensor([1,2,3]), torch.tensor([3, 4])
        # b = torch.nn.utils.rnn.pad_sequence(a, batch_first = True)
        # >> tensor([1,2,3],
        #           [3,4,0])
        # tensor.nn.utils.rnn.pack_padded_sequence(b, batch_size = True, lengths = [3, 2])
        # >> PackedSequence(data = tensor([1,3,2,4,3]), batch_sizes = tensor([2, 2, 1]))
        else:
            x = emb

        y, h = self.rnn(x)
        # |y| = (self, length, hidden_Size)
        # |h[0]| = (n_layers, batch_Size, hidden_size / 2)

        if isinstance(emb, tuple):
            y, _ = unpack(y, batch_first=True)

        return y, h

class Decoder(nn.Module):

    def __init__(self, word_vec_size, hidden_size, n_layers, dropout_p = .2):
        super(Decoder, self).__init__()

        self.rnn = nn.LSTM(
            word_vec_size + hidden_size,
            hidden_size,
            num_layers = n_layers,
            dropout = dropout_p,
            batch_first = True
        )

    def forward(self, emb_t, h_t_1_tilde, h_t_1):
        # |emb_t| = (batch_size, 1, word_vec_size)
        # |h_t_1_tilde| = (batch_size, 1, hidden_size)
        # |h_t_1[0]| = (n_layers, batch_size, hidden_size)

        batch_size = emb_t.size(0)
        hidden_size = h_t_1[0].size(-1) 

        if h_t_1_tilde is None:
            h_t_1_tilde = emb_t.new(batch_size, 1, hidden_size).zero_()

        # input_feeding
        x = torch.cat([emb_t, h_t_1_tilde], dim = -1)

        y, h = self.rnn(x, h_t_1)

        return y, h

class Generator(nn.Module):
    def __init__(self, hidden_size, output_size):
        super(Generator, self).__init__()

        self.output = nn.Linear(hidden_size, output_size)
        self.softmax = nn.LogSoftmax(dim = -1)

    def forward(self, x):
        # |x| = (batch_size, length, hidden_size)
        y = self.softmax(self.output(x))
        # |y| = (batch_size, length, output_size)
        return y
    

class Seq2Seq(nn.Module):

    def __init__(self, input_size, word_vec_size, hidden_size, output_size, n_layers = 4, dropout_p = .2):
        
        self.input_size = input_size
        self.word_vec_size = word_vec_size
        self.hidden_size = hidden_size
        self.output_size = output_size
        self.n_layers = n_layers
        self.dropout_p = dropout_p

        super(Seq2Seq,self).__init__()

        self.emb_src = nn.Embedding(input_size, word_vec_size)
        self.emb_dec = nn.Embedding(output_size, word_vec_size)

        self.encoder = Encoder(word_vec_size, hidden_size, n_layers,  dropout_p)
        self.decoder = Decoder(word_vec_size, hidden_size, n_layers, dropout_p)

        self.attn = Attention(hidden_size)

        self.concat = nn.Linear(hidden_size*2, hidden_size)

        self.tanh = nn.Tanh()

        self.generator = Generator(hidden_size, output_size)

    def generate_mask(self, x, length):
        # |x| = (bs, length)
        mask = []

        max_length = max(length)

        for l in length:
            if max_length - 1 > 0:
                mask += [torch.cat([x.new_ones(1, l).zero_(),
                                   x.new_ones(1, (max_length - l))], dim = -1)]
                
            else:
                mask += [x.new_ones(l, 1).zero_()]

        mask = torch.cat(mask, dim = 0).bool()

        return mask
    
    def merge_encoder_hiddens(self, encoder_hiddens):
        # |encoder_hiddens| = (n_layers*2, batch_size, hidden_size / 2)
        new_hiddens = []
        new_cells = []

        hiddens, cells = encoder_hiddens

        for i in range(0, hiddens.size(0), 2):
            new_hiddens += [torch.cat([hiddens[i] + hiddens[i+1]], dim = -1)]
            new_cells += [torch.cat([cells[i] + cells[i+1]], dim = -1)]

        new_hiddens, new_cells = torch.stack(new_hiddens), torch.stack(new_cells)

        return (new_hiddens, new_cells)
    
    def fast_merge_encoder_hiddens(self, encoder_hiddens):

        h_0_tgt, c_0_tgt = encoder_hiddens
        batch_size = h_0_tgt.size(1)

        h_0_tgt = h_0_tgt.transpose(0, 1).contiguous().view(batch_size, -1, self.hidden_size).transpose(0, 1).contiguous()
        c_0_tgt = c_0_tgt.transpose(0, 1).contiguous().view(batch_size, -1, self.hidden_size).transpose(0, 1).contiguous()

        return (h_0_tgt, c_0_tgt)
    

    def forward(self, src, tgt):
        
        batch_size = tgt.size(0)

        mask = None
        x_length = None

        if isinstance(src, tuple):
            x, x_length = src

            mask = self.generate_mask(x, x_length)

        else:
            x = src
        
        if isinstance(tgt, tuple):
            tgt = tgt[0]

        emb_src = self.emb_src(x)

        h_src, h_0_tgt = self.encoder((emb_src, x_length))
        #|h_src| = (batch_size, length, hidden_size)
        #|h_0_tgt| = (n_layers * 2, batch_size, hidden_size / 2)
        
        h_0_tgt = self.fast_merge_encoder_hiddens(h_0_tgt)

        emb_tgt = self.emb_dec(tgt)
        
        h_tilde = []
        h_t_tilde = None

        decoder_hidden = h_0_tgt

        for t in range(tgt.size(1)):
            
            emb_t = emb_tgt[:, t, :].unsqueeze(1)
            # |emb_t| = (batch_size, hidden_size) => (batch_size, 1, hidden_size)

            decoder_output, decoder_hidden = self.decoder(emb_t, h_t_tilde, decoder_hidden)
            # |decoder_output| = (batch_size, 1, hidden_size)
            # |decoder_hidden| = (n_layers, batch_size, hidden_size)

            context_vector = self.attn(h_src, decoder_output, mask)
            # |context_vector| = (batch_size, 1, hidden_size)
            h_t_tilde = self.tanh(self.concat(torch.cat([decoder_output, context_vector], dim = -1)))
            # |h_t_tilde| = (batch_size, 1, hidden_size)

            h_tilde += [h_t_tilde]

        h_tilde = torch.cat(h_tilde, dim = 1)

        y_hat = self.generator(h_tilde)
        # |y_hat| = |batch_size, length, output_size|
        return y_hat
    
    # def search(self, src, is_greedy = True, max_length = 255):
    #     if isinstance(src, tuple):
    #         x, x_length  = src
    #         mask = self.generate_mask(x, x_length)


    #     else:
    #         x, x_length = src, None
    #         mask = None

    #     batch_size = x.size(0)

    #     emb_src = self.emb_src(x)
    #     h_src, h_0_tgt = self.encoder((emb_src, x_length))
    #     decoder_hidden = self.fast_merge_encoder_hiddens(h_0_tgt)

    #     y = x.new(batch_size, 1).zero_() + data_loader.BOS

    #     is_decoding = x.new_ones(batch_size, 1).bool()
    #     h_t_tilde, y_hats, indice = None, [], []

    #     while is_decoding.sum() > 0 and len(indice) < max_length:
    #         emb_t = self.emb_dec(y)

    #         decoder_output, decoder_hidden = self.decoder(emb_t, h_t_tilde, decoder_hidden)
            
    #         context_vector = self.attn(h_src, decoder_output, mask)

    #         h_t_tilde = self.tanh(self.concat(torch.cat([decoder_output, context_vector], dim = -1)))

    #         y_hat = self.generator(h_t_tilde)

    #         y_hats += [y_hat]

    #         if is_greedy:
    #             y = y_hat.argmax(dim = -1)
    #             # |y| = (batch_size, 1)

    #         else:
    #             y = torch.multinomial(y_hat.exp().view(batch_size, -1), 1)
    #             # |y| = (batch_size, 1)

    #         y = y.masked_fill_(~is_decoding, data_loader.PAD)
    #         is_decoding = is_decoding * torch.ne(y, data_loader.EOS)

    #         indice += [y]

    #     y_hats = torch.cat(y_hats, dim = 1)
    #     indice = torch.cat(indice, dim = 1)

    #     return y_hats, indice

    def search(self, src, is_greedy=True, max_length=255):
        if isinstance(src, tuple):
            x, x_length = src
            mask = self.generate_mask(x, x_length)
        else:
            x, x_length = src, None
            mask = None
        batch_size = x.size(0)

        # Same procedure as teacher forcing.
        emb_src = self.emb_src(x)
        h_src, h_0_tgt = self.encoder((emb_src, x_length))
        decoder_hidden = self.fast_merge_encoder_hiddens(h_0_tgt)

        # Fill a vector, which has 'batch_size' dimension, with BOS value.
        y = x.new(batch_size, 1).zero_() + data_loader.BOS

        is_decoding = x.new_ones(batch_size, 1).bool()
        h_t_tilde, y_hats, indice = None, [], []
        
        # Repeat a loop while sum of 'is_decoding' flag is bigger than 0,
        # or current time-step is smaller than maximum length.
        while is_decoding.sum() > 0 and len(indice) < max_length:
            # Unlike training procedure,
            # take the last time-step's output during the inference.
            emb_t = self.emb_dec(y)
            # |emb_t| = (batch_size, 1, word_vec_size)

            decoder_output, decoder_hidden = self.decoder(emb_t,
                                                          h_t_tilde,
                                                          decoder_hidden)
            context_vector = self.attn(h_src, decoder_output, mask)
            h_t_tilde = self.tanh(self.concat(torch.cat([decoder_output,
                                                         context_vector
                                                         ], dim=-1)))
            y_hat = self.generator(h_t_tilde)
            # |y_hat| = (batch_size, 1, output_size)
            y_hats += [y_hat]

            if is_greedy:
                y = y_hat.argmax(dim=-1)
                # |y| = (batch_size, 1)
            else:
                # Take a random sampling based on the multinoulli distribution.
                y = torch.multinomial(y_hat.exp().view(batch_size, -1), 1)
                # |y| = (batch_size, 1)

            # Put PAD if the sample is done.
            y = y.masked_fill_(~is_decoding, data_loader.PAD)
            # Update is_decoding if there is EOS token.
            is_decoding = is_decoding * torch.ne(y, data_loader.EOS)
            # |is_decoding| = (batch_size, 1)
            indice += [y]

        y_hats = torch.cat(y_hats, dim=1)
        indice = torch.cat(indice, dim=1)
        # |y_hat| = (batch_size, length, output_size)
        # |indice| = (batch_size, length)

        return y_hats, indice