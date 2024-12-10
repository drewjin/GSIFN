import torch
import torch.nn.functional as F
from torch import nn
from torch.nn import Parameter


__all__ = ['GraphAttention']


class GraphAttention(nn.Module):
    def __init__(self, embed_dim, num_heads, attn_dropout=0.,
                 bias=True, add_bias_kv=False, add_zero_attn=False):
        super().__init__()
        self.embed_dim = embed_dim
        self.num_heads = num_heads
        self.attn_dropout = attn_dropout
        self.head_dim = embed_dim // num_heads
        assert self.head_dim * num_heads == self.embed_dim, "embed_dim must be divisible by num_heads"
        self.scaling = self.head_dim ** -0.5

        self.in_proj_weight = Parameter(torch.Tensor(3 * embed_dim, embed_dim))
        self.register_parameter('in_proj_bias', None)
        if bias:
            self.in_proj_bias = Parameter(torch.Tensor(3 * embed_dim))
        self.out_proj = nn.Linear(embed_dim, embed_dim, bias=bias)

        if add_bias_kv:
            self.bias_k = Parameter(torch.Tensor(1, 1, embed_dim))
            self.bias_v = Parameter(torch.Tensor(1, 1, embed_dim))
        else:
            self.bias_k = self.bias_v = None

        self.add_zero_attn = add_zero_attn

        self.reset_parameters()

    def reset_parameters(self):
        nn.init.xavier_uniform_(self.in_proj_weight)
        nn.init.xavier_uniform_(self.out_proj.weight)
        if self.in_proj_bias is not None:
            nn.init.constant_(self.in_proj_bias, 0.)
            nn.init.constant_(self.out_proj.bias, 0.)
        if self.bias_k is not None:
            nn.init.xavier_normal_(self.bias_k)
        if self.bias_v is not None:
            nn.init.xavier_normal_(self.bias_v)

    def forward(
        self, query_nodes, key_nodes, value_nodes, 
        edge_mask=None, seq_split=None, mask_fixer=None, direction=None, plot_map=False
    ):
        """Input shape: Time x Batch x Channel
        Self-attention can be implemented by passing in the same arguments for
        query, key and value. Timesteps can be masked by supplying a T x T mask in the
        `attn_mask` argument. Padding elements can be excluded from
        the key by passing a binary ByteTensor (`key_padding_mask`) with shape:
        batch x src_len, where padding elements are indicated by 1s.
        """
        qkv_same = query_nodes.data_ptr() == key_nodes.data_ptr() == value_nodes.data_ptr()
        kv_same = key_nodes.data_ptr() == value_nodes.data_ptr()

        tgt_len, bsz, embed_dim = query_nodes.size()
        assert embed_dim == self.embed_dim
        assert list(query_nodes.size()) == [tgt_len, bsz, embed_dim]
        assert key_nodes.size() == value_nodes.size()

        aved_state = None

        if qkv_same:
            # self-attention
            q, k, v = self.in_proj_qkv(query_nodes)
        elif kv_same:
            # encoder-decoder attention
            q = self.in_proj_q(query_nodes)

            if key_nodes is None:
                assert value_nodes is None
                k = v = None
            else:
                k, v = self.in_proj_kv(key_nodes)
        else:
            q = self.in_proj_q(query_nodes)
            k = self.in_proj_k(key_nodes)
            v = self.in_proj_v(value_nodes)
        q = q * self.scaling

        if self.bias_k is not None:
            assert self.bias_v is not None
            k = torch.cat([k, self.bias_k.repeat(1, bsz, 1)])
            v = torch.cat([v, self.bias_v.repeat(1, bsz, 1)])
            if edge_mask is not None:
                edge_mask = torch.cat([edge_mask, edge_mask.new_zeros(edge_mask.size(0), 1)], dim=1)

        q = q.contiguous().view(tgt_len, bsz * self.num_heads, self.head_dim).transpose(0, 1)
        if k is not None:
            k = k.contiguous().view(-1, bsz * self.num_heads, self.head_dim).transpose(0, 1)
        if v is not None:
            v = v.contiguous().view(-1, bsz * self.num_heads, self.head_dim).transpose(0, 1)

        src_len = k.size(1)

        if self.add_zero_attn:
            src_len += 1
            k = torch.cat([k, k.new_zeros((k.size(0), 1) + k.size()[2:])], dim=1)
            v = torch.cat([v, v.new_zeros((v.size(0), 1) + v.size()[2:])], dim=1)
            if edge_mask is not None:
                edge_mask = torch.cat([edge_mask, edge_mask.new_zeros(edge_mask.size(0), 1)], dim=1)
        
        # Decomposition: Naive Torch
        text, vision, audio = seq_split
        s1 = (0, text)                 # [0, t)
        s2 = (text, text + vision)             # [t, t + v)
        s3 = (text + vision, text + vision + audio)     # [t + v, t + v + a)
        attn_weights = []
        if direction == 'forward': 
            attn_weights.append( # v -> t
                torch.bmm(q[:, s1[0]:s1[1]], k[:, s2[0]:s2[1]].transpose(1, 2))
            ) 
            attn_weights.append( # a -> v
                torch.bmm(q[:, s2[0]:s2[1]], k[:, s3[0]:s3[1]].transpose(1, 2))
            )
            attn_weights.append( # t -> a
                torch.bmm(q[:, s3[0]:s3[1]], k[:, s1[0]:s1[1]].transpose(1, 2))
            )
        elif direction == 'backward':
            attn_weights.append( # a -> t
                torch.bmm(q[:, s1[0]:s1[1]], k[:, s3[0]:s3[1]].transpose(1, 2))
            ) 
            attn_weights.append( # t -> v
                torch.bmm(q[:, s2[0]:s2[1]], k[:, s1[0]:s1[1]].transpose(1, 2))
            )
            attn_weights.append( # v -> a
                torch.bmm(q[:, s3[0]:s3[1]], k[:, s2[0]:s2[1]].transpose(1, 2))
            )
        else:
            attn_weights.append( # a -> t
                torch.bmm(q[:, s1[0]:s1[1]], k[:, s1[0]:s1[1]].transpose(1, 2))
            ) 
            attn_weights.append( # t -> v
                torch.bmm(q[:, s2[0]:s2[1]], k[:, s2[0]:s2[1]].transpose(1, 2))
            )
            attn_weights.append( # v -> a
                torch.bmm(q[:, s3[0]:s3[1]], k[:, s3[0]:s3[1]].transpose(1, 2))
            )
        # TODO: Triton Kernel


        if mask_fixer is not None:
            attn_weights = (F.softmax(attn_weights.float(), dim=-1) * mask_fixer).type_as(attn_weights) 
        else:
            # attn_weights = F.softmax(attn_weights.float(), dim=-1).type_as(attn_weights)
            attn_weights = [
                F.softmax(attn_weight.float(), dim=-1).type_as(attn_weight)
                for attn_weight in attn_weights
            ]
        attn_weights = [
            F.dropout(attn_weight, p=self.attn_dropout, training=self.training)
            for attn_weight in attn_weights
        ]
    
        if direction == 'forward':
            attn_weights[0] = torch.bmm(attn_weights[0], v[:, s2[0]:s2[1]])
            attn_weights[1] = torch.bmm(attn_weights[1], v[:, s3[0]:s3[1]])
            attn_weights[2] = torch.bmm(attn_weights[2], v[:, s1[0]:s1[1]])
        elif direction == 'backward':
            attn_weights[0] = torch.bmm(attn_weights[0], v[:, s3[0]:s3[1]])
            attn_weights[1] = torch.bmm(attn_weights[1], v[:, s1[0]:s1[1]])
            attn_weights[2] = torch.bmm(attn_weights[2], v[:, s2[0]:s2[1]])
        else:
            attn_weights[0] = torch.bmm(attn_weights[0], v[:, s1[0]:s1[1]])
            attn_weights[1] = torch.bmm(attn_weights[1], v[:, s2[0]:s2[1]])
            attn_weights[2] = torch.bmm(attn_weights[2], v[:, s3[0]:s3[1]])
            
        attn = torch.concat(attn_weights, dim=1)

        attn = attn.transpose(0, 1).contiguous().view(tgt_len, bsz, embed_dim)
        attn = self.out_proj(attn)

        return attn, (None, None)

    def in_proj_qkv(self, query):
        return self._in_proj(query).chunk(3, dim=-1)

    def in_proj_kv(self, key):
        return self._in_proj(key, start=self.embed_dim).chunk(2, dim=-1)

    def in_proj_q(self, query, **kwargs):
        return self._in_proj(query, end=self.embed_dim, **kwargs)

    def in_proj_k(self, key):
        return self._in_proj(key, start=self.embed_dim, end=2 * self.embed_dim)

    def in_proj_v(self, value):
        return self._in_proj(value, start=2 * self.embed_dim)

    def _in_proj(self, input, start=0, end=None, **kwargs):
        weight = kwargs.get('weight', self.in_proj_weight)
        bias = kwargs.get('bias', self.in_proj_bias)
        weight = weight[start:end, :]
        if bias is not None:
            bias = bias[start:end]
        return F.linear(input, weight, bias)
    

if __name__ == '__main__':
    import math
    import numpy as np
    import seaborn as sns
    import matplotlib.pyplot as plt

    def plot(temp):
        plt.figure()
        mask_arr = np.array(temp)
        plt.figure(figsize=(10, 10), dpi=100)
        sns.heatmap(mask_arr, cbar=True)
        plt.show()   
    def build_adj_masked_matrix(split, mode='cross', direction='forward'):
        def get_mask_neginf_0(mask):
            neg_inf = -10e9
            return torch.where(
                mask == 0, neg_inf,
                torch.tensor(0, dtype=torch.float32)
            )
        
        t, v, a = split
        s1 = (0, t)                 # [0, t)
        s2 = (t, t + v)             # [t, t + v)
        s3 = (t + v, t + v + a)     # [t + v, t + v + a)
        sum_len = sum(split)
        mask_list = []
        for idx, split_len in enumerate(split):
            for _ in range(split_len):
                row_mask_tensor = torch.ones(sum_len, dtype=torch.float32)
                if idx == 0:
                    row_mask_tensor[0:s1[1]] = 0
                    if direction == 'forward':
                        row_mask_tensor[s3[0]:] = 0
                    elif direction == 'backward':
                        row_mask_tensor[s2[0]:s2[1]] = 0
                elif idx == 1:
                    row_mask_tensor[s2[0]:s2[1]] = 0
                    if direction == 'forward':
                        row_mask_tensor[s3[0]:] = 0
                    elif direction == 'backward':
                        row_mask_tensor[0:s1[1]] = 0
                elif idx == 2:
                    row_mask_tensor[s3[0]:s3[1]] = 0
                    if direction == 'forward':
                        row_mask_tensor[0:s1[1]] = 0
                    elif direction == 'backward':
                        row_mask_tensor[s2[0]:s2[1]] = 0
                mask_list.append(row_mask_tensor)
        if mode == 'cross':
            mask = torch.stack(mask_list)
            if direction == 'forward':
                return get_mask_neginf_0(mask)
            elif direction == 'backward':
                return get_mask_neginf_0(mask)
            else:
                raise ValueError(
                    'direction must be \'forward\' or \'backward\''
                )
        elif mode == 'self':
            return get_mask_neginf_0(
                torch.abs(torch.stack(mask_list) - 1)
            )
        else:
            raise ValueError(
                r'mode must be \'cross\' or \'self\''
            )

    split_lens = [50, 15, 46]
    mha = GraphAttention(embed_dim=768, num_heads=1)
    mask = build_adj_masked_matrix(split_lens, mode='cross', direction='backward')
    plot(mask)
    torch.manual_seed(11)
    input_1 = torch.randn(32, 50, 768)
    torch.manual_seed(12)
    input_2 = torch.randn(32, 15, 768)
    torch.manual_seed(13)
    input3 = torch.randn(32, 46, 768)
    input = torch.cat([input_1, input_2, input3], dim=1).permute(1, 0, 2)
    o = mha(query_nodes=input, key_nodes=input, value_nodes=input, edge_mask=mask, mask_fixer=None)
