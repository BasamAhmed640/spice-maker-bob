# Saving and checking the Bob key

The setup wizard accepts the **BOB API KEY** through a hidden prompt, stdin, or `--key-env NAME`, where NAME is an environment variable's name. The value itself must never be a command argument. The key is saved as plain local JSON in this copy's `data/credentials.bob.json`. It is **not encrypted**; anyone who can read this folder can read it. Keep the folder private and never share `data/`.

Open IBM Bob Shell once yourself to review and accept IBM's license before asking setup to verify the key. The application never accepts it on your behalf. With Internet access enabled, setup makes one short Bob Shell connection check with all tool groups disabled. It sends no datasheet or model. A successful authenticated inference reply is VERIFIED. An explicit authentication or permission refusal is REJECTED. A timeout, quota, connection, incomplete reply, missing Bob Shell, or model-configuration problem is UNVERIFIED; that result does not prove the key is invalid. Verification does not establish available quota or model accuracy and may consume a small amount of provider credit.

A rejected or unverified check makes the current setup attempt exit nonzero with a plain explanation. The saved key and chosen settings remain in this copy so you can correct the issue and rerun Setup. The application still refuses a build when Bob cannot be used. With Internet access off, setup saves the key without a network check and says so.

No key value is printed, logged, written to a model, sent to LTspice, or included in the source ZIP. To forget a saved key, delete `data/credentials.bob.json` inside this copy. The app has no telemetry; Bob Shell is a separate tool with its own settings.

See [IBM Bob Shell setup](https://bob.ibm.com/docs/shell/getting-started/install-and-setup) and [IBM Bob API keys](https://bob.ibm.com/docs/ide/account/api-keys).
