# Portable storage

Each extracted Spice Maker Bob folder is one independent copy. `Setup.cmd`, `Start.cmd`, and `Boardmodeler.cmd` resolve their own folder and set `SPICE_MAKER_ROOT`. The Python application writes its own files only inside that root:

| Path | Contents |
| --- | --- |
| `.venv/` | Python environment built from `requirements.txt` |
| `data/config.json` | LTspice path, relative model folder, Bob provider and Internet choice |
| `data/credentials.bob.json` | The Bob API key as plain local JSON |
| `data/temp/`, `data/logs/`, `data/plot-cache/` | Local scratch and diagnostics |
| `data/bob-profile/` | Isolated profile for Bob Shell |
| `models/` | Default models, evidence, caches, and simulator runs |
| `library/` | Local model and symbol exports |

The key is **not encrypted**. Anyone who can read this folder can read it. Keep the copy private and never share `data/`. No credential is placed in Windows Credential Manager, DPAPI, the registry, or an application profile. Several extracted copies do not share settings or keys.

Setup requires you to paste an existing LTspice executable path; it never searches for LTspice or adopts `LTSPICE_EXE`. It runs a smoke test and stores the path in `data/config.json`. The model folder must be inside this copy. Relative model paths continue to work after moving the folder; an LTspice path points to the existing installation and may need updating on another computer.

Setup downloads hash-checked wheels into `data/temp/` and installs exactly the pinned runtime packages into `.venv/`. When Python 3.14 is absent, it first asks whether to install a hash-checked python.org installer for the current Windows user. That Python installation is a system dependency outside this folder; it is not application state. LTspice, Bob Shell and datasheets are never downloaded by Setup.

The optional Desktop shortcut contains an absolute target to `Start.cmd`, so run Setup again after moving the extracted folder to refresh it. `Setup.cmd --remove` removes this copy's shortcut. A Desktop that cannot be written does not prevent setup. Delete the extracted folder to remove this app and its models. Remove Python separately in Windows Settings if Setup installed it and you no longer need it.

The Python write guard contains application-owned writes to this folder. It is not an operating-system sandbox: Bob Shell, LTspice, Python and Windows can maintain their own files, and Bob receives selected content when Internet access is enabled. Bob Shell runs with its tool groups disabled and returns model text; the application writes the candidate and judges it with LTspice.
