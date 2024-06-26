import logging, subprocess
import torch

from divan.utils.config import *

__all__ = ["chose_cuda"]

cuda_config = read_config(__file__)

def chose_cuda(device):
    if device == 'cuda' and torch.cuda.device_count() > 1:
        cuda_ram = subprocess.run(cuda_config['memory_utilization_command'].split(' '),encoding='utf-8',
                             stdout=subprocess.PIPE, stdin=subprocess.PIPE).stdout.replace('%', '').replace("MiB", "")
        cuda_ram = [int(i) for i in cuda_ram.split('\n')[1:] if len(i) > 0]
        device = f'{device}:{max(range(len(cuda_ram)), key=cuda_ram.__getitem__)}'
        logging.debug(f"{cuda_config['block_name']}: Auto choose - {device}")
        return device

    else:
        return device

if __name__ == '__main__':
    print(chose_cuda('cuda'))