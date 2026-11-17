# colab-cli (Windows clone)

Full clone of the Google Colab CLI built on the same MCP backend as your tested `colab_basic.py`.

##PLEASE NOTE THIS IS AN UNOFFICIAL COLAB CLI 

## Install (Windows)
1. Unzip anywhere, e.g. `C:\tools\colab-cli`
2. Double-click `install.bat` (or run `pip install -e .[readline]`)
3. Open a new terminal → `colab`

No install? Just run `colab.cmd ...` from the folder (needs `pip install -r requirements.txt`).

## First run
```
colab auth login              # gcloud ADC login (same creds colab_basic.py used)
colab auth --set-project gen-lang-client-0841150961
colab auth                    # check account / project / API reachability
```

## Quick tour
```
colab new                     # CPU runtime
colab new --gpu T4            # GPU   (colab new --list shows what your account can get)
colab sessions
colab status
colab exec -c "import torch; print(torch.cuda.is_available())"
colab exec train.py           # or notebook.ipynb --save out.ipynb
type script.py | colab exec   # stdin
colab repl
colab ssh                     # interactive shell   |  colab ssh -- nvidia-smi
colab upload data.csv  /  colab upload my_folder -d /content/proj
colab ls -l  /  colab download results/  /  colab rm -r tmp  /  colab edit config.yaml
colab install transformers accelerate
colab run --gpu T4 train.py --epochs 3        # fresh VM, auto teardown (--keep to keep it)
colab log --export session.ipynb              # .md .txt .jsonl too
colab restart-kernel  /  colab stop  /  colab stop --all
```

## Notes / honest limits of the MCP backend
- Output is returned when a cell finishes (no live streaming). Default wait 660 s; use `--timeout`.
- `ssh` is an emulated shell (cwd persists); full-screen apps (vim, top) and `--proxy` are not supported.
- `drivemount` needs Colab's interactive consent; if blocked, use `colab auth remote`.
- `usage` shows backend data only if the API exposes a usage tool; otherwise a local estimate.
- `url` prints a direct link only if the backend returns one; otherwise opens Colab.
- Hidden debug: `colab tools` lists backend tools; `--logtostderr` for HTTP traces.
- Config, sessions & logs: `%APPDATA%\colab-cli\`
