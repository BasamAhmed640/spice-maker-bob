# Spice Maker Bob

Spice Maker Bob turns a part number and datasheet PDF into an LTspice `.lib` model, `.asy` symbol, model card, and verification results. IBM Bob Shell proposes the model; LTspice runs the checks. A PASS applies only to a measured datasheet row at its recorded conditions. Unmeasured behavior stays UNKNOWN.

## Get started on Windows

1. On GitHub, choose **Code → Download ZIP** and extract the whole ZIP to a folder you can edit.
2. Double-click **Setup.cmd** in the extracted folder. Read its opening summary and answer its questions.
3. Double-click **Start.cmd** for the text menu. **Boardmodeler.cmd** runs flag commands.

Setup needs Windows 10 or 11 on x64 or ARM64. It looks for a real CPython 3.14 without running a `python` command from PATH. If none is found, it asks before downloading the pinned Python 3.14.7 installer from python.org, checking its SHA-256, and installing it for the current user without administrator rights or a PATH change. Declining stops setup. The installer and exact hashes are listed in [`tools/python-install-pins.txt`](tools/python-install-pins.txt).

Setup creates `.venv` in this folder and downloads eight pinned Python packages from PyPI. It checks every package hash before installation, installs wheels only, and honors `HTTPS_PROXY`. It does not download LTspice, IBM Bob Shell, a datasheet, or a vendor model. Install [IBM Bob Shell](https://bob.ibm.com/docs/shell/getting-started/install-and-setup) separately and open it once to review and accept IBM's license. Setup asks you to paste the path to an existing LTspice executable, runs a real smoke test, asks for a model folder inside this copy, and saves the Internet setting and your Bob API key. Bob Shell is the only provider in this edition, so the key prompt says **BOB API KEY**. The key prompt is hidden. The key is a plain local file at `data/credentials.bob.json`: anyone who can read this folder can read it. Keep the folder private, and do not share `data/`.

Setup offers an optional **Spice Maker** Desktop shortcut that runs `Start.cmd`. It asks before creating it. Re-running setup refreshes it; `Setup.cmd --remove` removes it. The shortcut is optional, so a locked-down Desktop does not prevent the app from working.

Windows may show a security prompt when you launch a script extracted from a downloaded ZIP. Check the source and contents of `Setup.cmd` before choosing to run it. If setup stops, its window stays open and prints what to fix. Use a short, writable extraction path; all app-owned data remains inside it.

To uninstall, run `Setup.cmd --remove` if you created the shortcut, then delete the extracted folder. If Setup installed Python for you, remove **Python 3.14** separately in **Windows Settings → Installed apps**. Deleting this copy does not uninstall LTspice or Bob Shell.

| Platform | Support | Observed verification |
| --- | --- | --- |
| Windows 11 x64 | Targeted | Full source ZIP setup, eight pinned packages, launchers, shortcut, and LTspice smoke check on the developer machine. The pinned x64 Python installer also passed on a fresh Windows GitHub Actions runner. |
| Windows 11 ARM64 | Targeted | The pinned ARM64 Python installer passed on a fresh GitHub Actions runner; full source ZIP setup was not tested there. |

Windows 10 x64/ARM64 are setup targets but were not tested in this change. See [`docs/STATUS.md`](docs/STATUS.md) for exact results and limits.

## Make and revisit a model

`Start.cmd` opens a menu to build a model, open or re-test a saved model, change settings, or check setup. A datasheet can be dragged into the prompt; quoted paths are accepted. You can also use the flag interface:

```text
Boardmodeler.cmd doctor --json
Boardmodeler.cmd setup --json
Boardmodeler.cmd model build --part TPS54332DDA --datasheet C:\path\to\sheet.pdf --out models\TPS54332DDA
Boardmodeler.cmd model open --out models\TPS54332DDA --json
Boardmodeler.cmd model test --out models\TPS54332DDA --json
```

Full electrical verification is the default. `model build --sanity` requests a quick structural draft; its electrical accuracy remains unverified. A missing LTspice path, Bob Shell, Bob key, or network permission blocks a build with its reason. No provider is substituted silently. Setup never searches this PC for LTspice; you provide its path.

Bob Shell receives its prompt through stdin with tool groups disabled. The application writes Bob's candidate and runs LTspice itself; Bob's reply cannot mark a model verified. With Internet access off, the app refuses Bob runs and key checks before launching Bob Shell. It has no telemetry. LTspice receives an allowlisted child environment without the Bob API key. The app writes its own files inside the extracted folder, never into the LTspice installation or library.

For the exact storage paths and key tradeoff, see [`docs/PORTABLE_STORAGE.md`](docs/PORTABLE_STORAGE.md). For model coverage and limits, see [`docs/FAST_ACCURATE_MODELS.md`](docs/FAST_ACCURATE_MODELS.md).

## Setup options

`Setup.cmd --yes` uses saved answers, and `--no-install-python` refuses a Python download. Pass `--ltspice`, `--model-dir`, `--provider bob`, `--internet on|off`, `--shortcut yes|no`, and `--shortcut-dir` for scripted setup. `--key-env NAME` names an environment variable containing the Bob API key; the key value itself never belongs on a command line. `Setup.cmd --remove` removes the shortcut made by this copy.

## Development

Source development requires Python 3.14. The user setup requires only the tools that ship with Windows.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements.txt
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check .
```

`uv.lock` is the developer lock. `requirements.txt` is its hash-bearing runtime export and has no editable project install. The launchers put `src` on `PYTHONPATH` instead. [`AGENTS.md`](AGENTS.md) contains the repository rules. `pyproject.toml` currently declares **Proprietary**; the owner has not chosen publication license terms yet.
