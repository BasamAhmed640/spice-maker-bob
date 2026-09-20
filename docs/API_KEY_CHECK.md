# Saving and checking a key

SETUP's **SAVE & CHECK KEY** stores the entered key in Windows Credential Manager,
clears the password field, and starts a background connection check. The window
stays responsive and stops waiting after 15 seconds. Closing SETUP or changing
provider discards the old check's result.

The check sends only a short request to acknowledge the connection. It does not
send a datasheet or model. One request is made, with no automatic retries; it may
use a small amount of provider credit. The selected provider, model and reasoning
settings are retained. HTTP verification caps the response budget at 256 tokens;
exhausting this small budget can still establish that the service accepted the
key and selected model. This is not a model-accuracy or available-quota guarantee.

- VERIFIED: an authenticated inference response was observed.
- REJECTED: the provider refused authentication or permissions.
- UNVERIFIED: connection, timeout, quota, model configuration, incomplete response,
  or a missing dependency prevented a conclusion. This does not mean the key is invalid.

The key remains saved after an inconclusive or rejected check so the user can
resolve account or connection issues. Paste a replacement and save again to retry.
Verification is not a prerequisite that blocks a later model build.

IBM Bob verification uses Bob Shell 2.x with a one-turn limit, a 0.05 Bobcoin cap,
an empty temporary workspace, and all documented tool groups disabled. IBM's
license must first be accepted by the user. An Inference API key avoids needing
the additional team ID required by General keys. The check never accepts the
license automatically and never puts the key in command arguments.

Keys are saved in the Windows user's credential store, not in the repository,
settings JSON, model output, installer or ZIP. Updates reuse those local entries;
downloads on another computer have no saved key. Credentials are protected by the
Windows account, not isolated from every application running as that same user.
Keep the Windows account and device protected.

HTTPS uses certificate verification. Credential-bearing HTTP requests do not follow
redirects. The setup UI shows fixed diagnostic messages rather than provider response
bodies or exception strings that could echo a secret. No Windows security policy
needs to be disabled for credential checking.

References: [Windows Credential Manager](https://support.microsoft.com/en-us/windows/security/credential-manager-in-windows),
[IBM Bob API keys](https://bob.ibm.com/docs/ide/account/api-keys),
[Bob non-interactive sessions](https://bob.ibm.com/docs/shell/getting-started/start-bobshell-non-interactive),
[Bob settings and optional telemetry](https://bob.ibm.com/docs/shell/configuration/configuring).
