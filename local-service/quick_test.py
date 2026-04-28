#!/usr/bin/env python3
"""Quick test: verify model loading works"""
import os
import sys

os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
os.environ['HF_HUB_DISABLE_XET'] = '1'

# Set up immediate stderr
import logging
logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)

print('Step 1: Importing transformers...', flush=True)
sys.stderr.write('STDERR: Step 1 importing...\n')
sys.stderr.flush()

try:
    from transformers import Qwen2VLForConditionalGeneration, AutoProcessor
    print('Step 1b: import OK', flush=True)
except Exception as e:
    print(f'IMPORT ERROR: {e}', flush=True)
    sys.exit(1)

print('Step 2: Loading processor...', flush=True)
sys.stderr.write('STDERR: Step 2 loading processor...\n')
sys.stderr.flush()

try:
    processor = AutoProcessor.from_pretrained(
        'Qwen/Qwen2-VL-2B-Instruct',
        trust_remote_code=True
    )
    print(f'Step 3: Processor loaded OK - type={type(processor)}', flush=True)
except Exception as e:
    print(f'PROCESSOR ERROR ({type(e).__name__}): {e}', flush=True)
    import traceback
    traceback.print_exc()
    # Don't exit, try model directly
    pass

print('Step 4: Loading model...', flush=True)
try:
    model = Qwen2VLForConditionalGeneration.from_pretrained(
        'Qwen/Qwen2-VL-2B-Instruct',
        torch_dtype='auto',
        trust_remote_code=True,
    )
    print(f'Step 5: Model loaded OK - dtype={model.dtype}', flush=True)

    import torch
    if torch.backends.mps.is_available():
        model = model.to('mps')
        print(f'Step 6: Moved to MPS', flush=True)

    print(f'\n✅ SUCCESS! Device={next(model.parameters()).device}', flush=True)
except Exception as e:
    print(f'MODEL ERROR ({type(e).__name__}): {e}', flush=True)
    import traceback
    traceback.print_exc()
    sys.exit(1)

