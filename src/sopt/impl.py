import multiprocessing
from x_transformers import XTransformer
import numpy as np
from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
from  subprocess import PIPE, Popen, run
import os
import sentencepiece as spm
import random
import sys
import string
import platform
import ast
import csv
import gzip
import base64
import shutil
import time
from functools import wraps
import torch
import tqdm

from util import randstring

ARCH = 'x86'

if torch.cuda.is_available():
  DEVICE = 'cuda'
  WORLD_SIZE = torch.cuda.device_count()
else:
  DEVICE = 'cpu'
  WORLD_SIZE = 1



DTYPE = torch.float16 if '2060' in torch.cuda.get_device_name() else torch.bfloat16
ROOTDIR = os.path.abspath(os.path.join(os.path.dirname(__file__),'..','..'))
TMP = '/tmp/sopt'
DICTIONARY = f'{ROOTDIR}/misc/zstd_x86_dictionary'
CHECKPOINT = f'/{ROOTDIR}/checkpoint-{torch.cuda.get_device_name()}.pt'
GENERATE_EVERY = 100
LEARNING_RATE = 1e-4
NUM_BATCHES = int(1e5)
NUM_TOKENS = 32768 + 2
ENC_SEQ_LEN = 2048
DEC_SEQ_LEN = 2048
BATCH_SIZE = 8



if platform.system() == 'Linux':
  if ARCH == 'riscv':
    CC = 'riscv64-linux-gnu-gcc'
    STRIP = 'riscv64-linux-gnu-strip'
    OBJDUMP = 'riscv64-linux-gnu-objdump'
  elif ARCH == 'x86':
    CC = 'gcc'
    STRIP = 'strip'
    OBJDUMP = 'objdump'
    OBJCOPY = 'objcopy'
elif platform.system() == 'Darwin':
  if ARCH == 'riscv':
    CC = 'riscv64-elf-gcc'
    STRIP = 'riscv64-elf-strip'
    OBJDUMP = 'riscv64-elf-objdump'
  elif ARCH == 'x86':
    CC = 'x86_64-elf-gcc'
    STRIP = 'x86_64-elf-strip'
    OBJDUMP = 'x86_64-elf-objdump'
    OBJCOPY = 'x86_64-elf-objcopy'
  elif ARCH == 'aarch64':
    CC = 'aarch64-elf-gcc'
    STRIP = 'aarch64-elf-strip'
    OBJDUMP = 'aarch64-elf-objdump'


def tkn_sp(t):
  if t == 'DECSTART':
    return 32768
  elif t == 'PAD':
    return 32769
  assert False

def tkn_char(t):
  if t == 'DECSTART':
    return 257
  elif t == 'PAD':
    return 256
  assert False

def zstd_train():
  os.makedirs(f'/{TMP}/all_objs', exist_ok=True)
  for db_idx in range(len(os.listdir(f'/{ROOTDIR}/cleandata/')))[:25]:
    with gzip.open(f'/{ROOTDIR}/cleandata/processed_{db_idx}.csv.gz', 'rt') as f:
      reader = csv.DictReader(f)
      for entry in reader:
        unopt = ast.literal_eval(entry['unopt'])
        opt = ast.literal_eval(entry['opt'])
        with open(f'/{TMP}/all_objs/{randstring(16)}.o','w+b') as outf:
          outf.write(unopt)
        with open(f'/{TMP}/all_objs/{randstring(16)}.o','w+b') as outf:
          outf.write(opt)

def sentencepiece_train():
  with open(f'{TMP}/sentencepiece.txt', 'w+b') as outf:
    for db_idx in range(len(os.listdir(f'/{ROOTDIR}/cleandata/'))):
      with gzip.open(f'/{ROOTDIR}/cleandata/processed_{db_idx}.csv.gz', 'rt') as f:
        reader = csv.DictReader(f)
        for entry in reader:
          unopt = ast.literal_eval(entry['unopt'])
          opt = ast.literal_eval(entry['opt'])
          outf.write(unopt)
          outf.write(opt)

sp = None

def tokenize_sp(data: bytes):
  global sp
  if sp is None:
    sp = spm.SentencePieceProcessor()
    sp.load(f'{ROOTDIR}/misc/x86_sopt_32k.model')
  tokens = sp.encode(base64.b64encode(data).decode('utf-8'))
  return tokens


def detokenize_sp(tokens: [int]):
  global sp
  if sp is None:
    sp = spm.SentencePieceProcessor()
    sp.load(f'{ROOTDIR}/misc/x86_sopt_32k.model')
  tokens = [t for t in tokens if t < NUM_TOKENS-2]
  tokens = sp.decode(tokens)
  try:
    tokens = base64.b64decode(tokens)
  except:
    tokens = "invalid".encode('utf-8')
  return tokens

def tokenize_char(data: bytes):
  '''0-255 encode that data
     256 = PAD
     257 = DECSTART
     258 - 511 = tokenval - 2 zeroes in a row
  '''
  ret = []
  nzero = 0
  for b in list(data):
    if b != 0:
      if nzero == 0:
        ret.append(b)
      elif nzero == 1:
        ret.append(0)
        ret.append(b)
        nzero = 0
      else:
        ret.append(nzero + 256)
        ret.append(b)
        nzero = 0
    else:
      if nzero == 254:
        ret.append(511)
        nzero = 0
      else:
        nzero +=1
  if nzero > 0:
    ret.append(nzero + 256)
  return ret

def detokenize_char(tokens: [int]):
  ret = []
  for t in tokens:
    if 0 <= t <= 255:
      ret.append(t)
    elif 258 <= t <= 511:
      ret.extend([0] * (t - 256))
    else:
      pass #no need to pass meta tokens to detokenize
  return bytes(ret)

def zstd_compress(data: bytes, dictionary: str) -> bytes:
  ret = run(f"zstd -D {dictionary} --ultra -22 -c -".split(), input=data,  stdout=PIPE, stderr=PIPE)
  return ret.stdout

def zstd_decompress(data: [int], dictionary: str) -> bytes:
  data = [x for x in data if 0 <= x <= 255]
  ret = run(f"zstd -D {dictionary} --ultra -22 -d -c -".split(), input=bytes(data),  stdout=PIPE, stderr=PIPE)
  if len(ret.stderr) > 0:
    return ret.stderr + bytes(data)
  return ret.stdout


def gen(args):
  uuid, all_inputs = args
  outpath = f'/{ROOTDIR}/rawdata/db_{randstring(16)}.csv.gz'
  with gzip.open(outpath,'w+t') as f:
    writer = csv.DictWriter(f,['c','unopt','opt'])
    writer.writeheader()
    for x in range(100):
      if uuid == 0 and x % 10 == 0:
        print(x)

      func_c = f'/{TMP}/yarpgen_{uuid}/func.c'
      unopt_o = f'/{TMP}/yarpgen_{uuid}/func.c.unopt.o'
      opt_o = f'/{TMP}/yarpgen_{uuid}/func.c.opt.o'

      yarpgen = run(f'/{ROOTDIR}/bin/{platform.system()}/yarpgen --std=c -o /{TMP}/yarpgen_{uuid}'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      unoptgcc = run(f'{CC} -o {unopt_o} -O0 -Wall -fcf-protection=none -fno-asynchronous-unwind-tables -fno-unwind-tables -march=znver3 -xc -c {func_c}'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      unoptclang = run(f'clang -o {unopt_o} -O0 -Wall -fcf-protection=none -fno-asynchronous-unwind-tables -fno-unwind-tables -march=znver3 -xc -c {func_c}'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)

      optgcc = run(f'{CC} -o {opt_o} -O3 -Wall -fcf-protection=none -fno-asynchronous-unwind-tables -fno-unwind-tables -march=znver3 -xc -c {func_c}'.split(),stdin=PIPE, stdout=PIPE, stderr=PIPE)
      optclang = run(f'clang -o {opt_o} -O3 -Wall -fcf-protection=none -fno-asynchronous-unwind-tables -fno-unwind-tables -march=znver3 -xc -c {func_c}'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)

      unopt = run(f'{STRIP} {unopt_o}'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      opt = run(f'{STRIP} {opt_o}'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      unopt = run(f'{OBJCOPY} --remove-section .comment {unopt_o}'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      opt = run(f'{OBJCOPY} --remove-section .comment {opt_o}'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
      with  open(func_c) as f:
        prog = f.read()
      with open(unopt_o, 'rb') as f:
        unopt = f.read()
      with open(opt_o, 'rb') as f:
        opt = f.read()

      if h := hash(unopt) in all_inputs:
        continue
      all_inputs.add(h)
      if len(unopt) > 16384 or len(opt) > 16384:
        print("skipping too long prog")
        continue
      writer.writerow({'c': prog, 'unopt': unopt, 'opt': opt})
  return outpath

def clean_database(files, all_inputs):
  print("cleaning database")
  i = len(os.listdir(f'/{ROOTDIR}/cleandata'))
  for gz in files:
    print(f"cleaning {gz}")
    out = list()
    with gzip.open(gz, 'rt') as inf:
      reader = csv.DictReader(inf)
      for row in reader:
        if h := hash(row['unopt']) not in all_inputs:
          all_inputs.add(h)
          out.append(row)
    with gzip.open(f'/{ROOTDIR}/cleandata/processed_{i}.csv.gz', 'w+t') as outf:
      writer = csv.DictWriter(outf, ['c', 'unopt', 'opt'])
      writer.writeheader()
      writer.writerows(out)
      i+=1
  return all_inputs


def generate_database():
  ALL_INPUTS = set()
  print("generating database")
  ncpu = multiprocessing.cpu_count()
  os.makedirs(f'{TMP}/data', exist_ok=True)
  os.makedirs(f'{TMP}/all_yarpgen', exist_ok=True)
  os.makedirs(f'{ROOTDIR}/rawdata', exist_ok=True)
  os.makedirs(f'{ROOTDIR}/cleandata', exist_ok=True)
  for uuid in range(ncpu):
    os.makedirs(f'{TMP}/yarpgen_{uuid}', exist_ok=True)
  print(f"spawning {ncpu} threads")
  for x in range(100):
    print('processed', x)
    with multiprocessing.Pool(ncpu) as p:
      args = []
      for x in range(ncpu):
        args.append((x,ALL_INPUTS))
      ret = p.map(gen, args)
    #ret = gen(0)
    ALL_INPUTS = clean_database(ret, ALL_INPUTS)

def save_checkpoint(model,  optim, loss, scaler, scheduler):
  with FSDP.summon_full_params(model, writeback=False, recurse=False):
    if DEVICE == 'cuda':
      torch.save({
        'model_state_dict': model.state_dict(),
        'optimizer_state_dict': optim.state_dict(),
        'loss': loss.item(),
        'scaler': scaler.state_dict(),
        'scheduler': scheduler.state_dict()},
        CHECKPOINT)

def load_checkpoint(model, optim, loss):
  if os.path.exists(CHECKPOINT):
    print(f"loading {CHECKPOINT}")
    checkpoint = torch.load(CHECKPOINT)
    model.load_state_dict(checkpoint['model_state_dict'])
    optim.load_state_dict(checkpoint['optimizer_state_dict'])
    loss = checkpoint['loss']
  return model, optim,  loss