import copy
import os
import os.path as osp

import hydra
import torch
from omegaconf import DictConfig, OmegaConf

import matplotlib.pyplot as plt

from cprnn.utils import get_yaml_dict


def satisfies_conditions(root_a, root_b, catchall='any'):

    for key, value in root_a.items():
        if isinstance(value, dict):
            if not satisfies_conditions(root_a[key], root_b[key]):
                return False
        elif isinstance(root_b[key], list):
            if root_a[key] not in root_b[key]:
                return False
        else:
            if root_a[key] != root_b[key] and root_b[key] != catchall :
                return False
        # else:
        #     if root_a[key] != root_b[key] and root_b[key] != catchall and (isinstance(root_b[key], list) and root_a[key] not in root_b[key]):
        #         return False

    return True


def get_leaf_value(root, addr_string):
    keys = addr_string.split('.')
    value = copy.deepcopy(root)
    while len(keys) > 0:
        value = value[keys.pop(0)]
    return value


@hydra.main(version_base=None, config_path="./", config_name="visualization_configs_hidden")
def main(cfg: DictConfig) -> None:

    args = OmegaConf.to_container(cfg, resolve=True)
    for key, value in args.items():
        print(key + " : " + str(value))

    groups = dict()
    groups_r = dict()
    min_epochs = 2  # todo: change this quick fix
    for filename in os.listdir(args['visualization']['root']):

        dct = torch.load(
            osp.join(args['visualization']['root'], filename, 'model_best.pth'), map_location=torch.device('cpu')
        )
        exp_cfg = get_yaml_dict(osp.join(args['visualization']['root'], filename, "configs.yaml"))

        # Configuring what to include in plot
        if not satisfies_conditions(exp_cfg, args, catchall='any') or dct['epoch'] < min_epochs:
            print("  Skipping: {} | Epochs: {}".format(filename, dct['epoch']))
            continue

        bpc = dct['test_metrics']['bpc']
        params = dct['num_params']
        rank = dct['config']['model']['rank']
        hidden = dct['config']['model']['hidden_size']
        model_name = dct['config']['model']['name']




        print("Exp: {} | Epochs: {}".format(filename, dct['epoch']))

        # group_name = ", ".join([str(get_leaf_value(exp_cfg, attr_name)) for attr_name in args['visualization']['group_by']])
        group_name = []
        for attr_name in args['visualization']['group_by']:
            if model_name == "2rnn" or model_name == "mirnnnew":
                group_name.append(get_leaf_value(exp_cfg, attr_name))
                break
            else:
                group_name.append(get_leaf_value(exp_cfg, attr_name))
        group_name = tuple(group_name)
        print(group_name)

        if group_name not in groups:
            groups[group_name] = ([rank], [bpc], [hidden], [model_name])

        else:
            groups[group_name][0].append(rank)
            groups[group_name][1].append(bpc)
            groups[group_name][2].append(hidden)
            groups[group_name][3].append(model_name)

    for group_name, (rank, bpc, hidden, model_name) in groups.items():
        if group_name[0] == '2rnn' or group_name[0] == 'mirnnnew':
            plt.scatter(hidden, bpc, label=group_name[0])
        elif group_name[1] in [64,256,1024]:
            plt.scatter(hidden, bpc, label=group_name)

        # plt.scatter(hidden, bpc, label=group_name[0])

    plt.xlabel('Hidden size')
    plt.xscale("log")
    plt.ylabel('BPC')
    plt.legend()
    plt.savefig(args['visualization']['output_filename'])


if __name__ == "__main__":
    main()