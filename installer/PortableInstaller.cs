using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8")]

/// <summary>
/// Folder-local setup. Everything it writes is inside the folder that holds this
/// executable: app/ (the frozen GUI), env/ (the vendored Python runtime and wheel set),
/// .venv (this copy's own environment, created here), Start.cmd, Boardmodeler.cmd and
/// one .lnk. No registry, Start Menu, Desktop or AppData state is created, and another
/// copy of the same edition in another folder is never read or written.
/// </summary>
internal static class PortableInstaller {
    private const string EditionMarker = ".spice-maker-root";
    private const string LockFile = ".install.lock";
    private const string AppFolder = "app";
    private const string EnvFolder = "env";
    private const string VenvFolder = ".venv";
    private const string SetupLog = ".setup.log";
    private const string SetupError = ".venv-setup-error.txt";

    private static string Root;
    private static string Edition;
    private static string Version = "";
    private static string EnvironmentWarning;
    private static int Result;
    private static bool Silent;

    [STAThread]
    private static int Main(string[] args) {
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        Root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        Silent = Array.IndexOf(args, "--silent") >= 0;
        bool launch = Array.IndexOf(args, "--no-launch") < 0;
        Edition = Resource("edition.txt");
        Version = Resource("version.txt");
        Application.EnableVisualStyles();
        Log("== " + Edition + " " + Version + " setup in " + Root);
        if (Silent) {
            try { Install(); } catch (Exception ex) { Fail(ex); }
        } else {
            using (var form = new Form())
            using (var gif = Assembly.GetExecutingAssembly().GetManifestResourceStream("splash.gif")) {
                form.Text = Edition + " " + Version + " setup";
                form.FormBorderStyle = FormBorderStyle.FixedDialog;
                form.MaximizeBox = false; form.MinimizeBox = false;
                form.StartPosition = FormStartPosition.CenterScreen;
                var picture = new PictureBox { Image = Image.FromStream(gif), SizeMode = PictureBoxSizeMode.AutoSize };
                form.ClientSize = picture.Image.Size;
                form.Controls.Add(picture);
                bool done = false;
                form.FormClosing += (s,e) => { if (!done) e.Cancel = true; };
                form.Shown += async (s,e) => {
                    try { await Task.Run((Action)Install); } catch (Exception ex) { Fail(ex); }
                    finally { done = true; form.Close(); }
                };
                Application.Run(form);
            }
            if (EnvironmentWarning != null)
                MessageBox.Show(EnvironmentWarning, Edition + " setup", MessageBoxButtons.OK, MessageBoxIcon.Warning);
        }
        if (Result == 0 && launch) {
            try {
                Process.Start(new ProcessStartInfo(Safe(AppFolder + "/SpiceMaker.exe")) {
                    WorkingDirectory = Root, UseShellExecute = false
                });
            } catch (Exception ex) { Fail(ex); }
        }
        return Result;
    }

    private static string Resource(string name) {
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name)) {
            if (stream == null) return "";
            using (var reader = new StreamReader(stream)) return reader.ReadToEnd().Trim();
        }
    }

    private static void Log(string message) {
        try { File.AppendAllText(Safe(SetupLog), DateTime.UtcNow.ToString("s") + "Z " + message + "\r\n"); }
        catch (Exception) { }
    }

    private static string Safe(string relative) {
        var value = Path.GetFullPath(Path.Combine(Root, relative));
        if (!value.StartsWith(Root.TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new IOException("An installer path escapes the extracted folder.");
        return value;
    }

    private static void DeleteTree(string path) {
        path = Safe(path);
        if (!Directory.Exists(path)) return;
        if ((File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("Refusing to remove a linked folder: " + path);
        foreach (string child in Directory.GetDirectories(path)) DeleteTree(child);
        foreach (string file in Directory.GetFiles(path)) File.Delete(Safe(file));
        Directory.Delete(path);
    }

    private static void Extract(string resource, string destination) {
        Directory.CreateDirectory(destination);
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(resource))
        using (var archive = new ZipArchive(stream, ZipArchiveMode.Read)) {
            foreach (var entry in archive.Entries) {
                var path = Path.GetFullPath(Path.Combine(destination, entry.FullName));
                if (!path.StartsWith(destination + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                    throw new IOException("Invalid archive path.");
                if (String.IsNullOrEmpty(entry.Name)) { Directory.CreateDirectory(path); continue; }
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                using (var input = entry.Open())
                using (var output = File.Create(path)) input.CopyTo(output);
            }
        }
    }

    private static void Install() {
        var marker = Safe(EditionMarker);
        if (File.Exists(marker) && File.ReadAllText(marker).Trim() != Edition)
            throw new IOException("This folder belongs to a different edition. Extract each edition into its own folder.");
        using (var installLock = new FileStream(Safe(LockFile), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None)) {
            string staging = Safe(".install-" + Guid.NewGuid().ToString("N"));
            try {
                Stage(staging);
                Replace(staging);
                File.WriteAllText(marker, Edition);
                WriteLaunchers();
            } finally {
                if (Directory.Exists(staging)) DeleteTree(staging);
            }
        }
        File.Delete(Safe(LockFile));
        ProvisionEnvironment();
    }

    /// <summary>Unpack both payloads beside the installer before anything is replaced.</summary>
    private static void Stage(string staging) {
        Directory.CreateDirectory(staging);
        Extract("payload.zip", Path.Combine(staging, AppFolder));
        Extract("env.zip", Path.Combine(staging, EnvFolder));
        if (!File.Exists(Path.Combine(staging, AppFolder, "SpiceMaker.exe")))
            throw new IOException("The app is missing from the payload.");
        if (!File.Exists(Path.Combine(staging, EnvFolder, "python", "python.exe")))
            throw new IOException("The Python environment is missing from the payload.");
        Log("staged " + AppFolder + "/ and " + EnvFolder + "/");
    }

    /// <summary>Move both staged folders into place, keeping a copy of what they replace.</summary>
    private static void Replace(string staging) {
        var undo = new List<Action>();
        string backup = Safe(".previous-" + Guid.NewGuid().ToString("N"));
        try {
            foreach (string name in new[] { AppFolder, EnvFolder }) {
                string target = Safe(name);
                string source = Path.Combine(staging, name);
                if (Directory.Exists(target)) {
                    if ((File.GetAttributes(target) & FileAttributes.ReparsePoint) != 0)
                        throw new IOException("The " + name + " folder must not be a link.");
                    if (name == AppFolder) EnsureNotRunning(target);
                    string saved = backup + "-" + name;
                    Directory.Move(target, saved);
                    string restore = target, from = saved;
                    undo.Add(delegate { Directory.Move(from, restore); });
                }
                Directory.Move(source, target);
                string created = target;
                undo.Add(delegate { if (Directory.Exists(created)) DeleteTree(created); });
            }
        } catch (Exception) {
            for (int index = undo.Count - 1; index >= 0; index--) {
                try { undo[index](); } catch (Exception rollback) { Log("rollback failed: " + rollback.Message); }
            }
            throw;
        }
        foreach (string name in new[] { AppFolder, EnvFolder })
            if (Directory.Exists(backup + "-" + name)) DeleteTree(backup + "-" + name);
        Log("installed " + AppFolder + "/ and " + EnvFolder + "/");
    }

    private static void EnsureNotRunning(string app) {
        foreach (var file in Directory.GetFiles(app, "*.exe", SearchOption.TopDirectoryOnly))
            using (File.Open(file, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { }
    }

    private static void WriteLaunchers() {
        var start = new StringBuilder();
        start.Append("@echo off\r\n");
        start.Append("rem Starts this copy of " + Edition + " from its own folder.\r\n");
        start.Append("rem %~dp0 is this folder, so the launcher keeps working if the folder is moved.\r\n");
        start.Append("start \"\" /D \"%~dp0\" \"%~dp0" + AppFolder + "\\SpiceMaker.exe\" %*\r\n");
        File.WriteAllText(Safe("Start.cmd"), start.ToString(), Encoding.ASCII);

        var cli = new StringBuilder();
        cli.Append("@echo off\r\n");
        cli.Append("rem The command line of this copy, run from this copy's own environment (.venv).\r\n");
        cli.Append("setlocal\r\n");
        cli.Append("if not exist \"%~dp0" + VenvFolder + "\\Scripts\\python.exe\" (\r\n");
        cli.Append("  echo This copy has no " + VenvFolder + " yet. Run Install.exe in this folder to create it.\r\n");
        cli.Append("  exit /b 2\r\n");
        cli.Append(")\r\n");
        cli.Append("if \"%~1\"==\"\" (\r\n");
        cli.Append("  echo Usage: Boardmodeler.cmd ^<command^> -- for example: version or doctor --json\r\n");
        cli.Append("  echo This environment has no Qt. Commands that open a window - ui and setup - are\r\n");
        cli.Append("  echo served by the bundled app: run Start.cmd or app\\SpiceMaker.exe --cli ^<command^>.\r\n");
        cli.Append(")\r\n");
        cli.Append("set \"SPICE_MAKER_ROOT=%~dp0\"\r\n");
        cli.Append("\"%~dp0" + VenvFolder + "\\Scripts\\python.exe\" -m boardmodeler.cli %*\r\n");
        File.WriteAllText(Safe("Boardmodeler.cmd"), cli.ToString(), Encoding.ASCII);

        WriteShortcut("Spice Maker.lnk");
    }

    /// <summary>
    /// A folder-local shortcut. Windows stores the absolute target, so moving the folder
    /// leaves the .lnk behind; Start.cmd resolves its own folder and is the move-safe
    /// launcher. Re-running setup refreshes the .lnk. No Start Menu or Desktop copy.
    /// </summary>
    private static void WriteShortcut(string name) {
        string link = Safe(name);
        object shell = null;
        try {
            var type = Type.GetTypeFromProgID("WScript.Shell");
            if (type == null) { Log("shortcut: WScript.Shell is unavailable; Start.cmd is the launcher"); return; }
            shell = Activator.CreateInstance(type);
            object item = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { link });
            var itemType = item.GetType();
            Set(itemType, item, "TargetPath", Safe("Start.cmd"));
            Set(itemType, item, "WorkingDirectory", Root);
            Set(itemType, item, "Description", Edition + " " + Version + " (this folder)");
            Set(itemType, item, "IconLocation", Safe(AppFolder + "\\SpiceMaker.exe") + ",0");
            itemType.InvokeMember("Save", BindingFlags.InvokeMethod, null, item, null);
            Log("shortcut: wrote " + name);
        } catch (Exception ex) {
            Log("shortcut: " + ex.Message);
        } finally {
            if (shell != null && Marshal.IsComObject(shell)) Marshal.ReleaseComObject(shell);
        }
    }

    private static void Set(Type type, object item, string property, string value) {
        type.InvokeMember(property, BindingFlags.SetProperty, null, item, new object[] { value });
    }

    /// <summary>
    /// Create this copy's own interpreter environment once, from the vendored runtime and
    /// wheel set, with no package index involved. Nothing outside this folder is read or
    /// written, and an existing environment is kept as it is.
    /// </summary>
    private static void ProvisionEnvironment() {
        string venv = Safe(VenvFolder);
        string python = Safe(VenvFolder + "/Scripts/python.exe");
        bool created = false;
        try {
            if (File.Exists(python)) {
                Log("environment: keeping the existing " + VenvFolder);
                if (Check(python) == 0) { ClearWarning(); return; }
                throw new IOException("The existing " + VenvFolder + " cannot import the application.");
            }
            if (Directory.Exists(venv))
                throw new IOException("The " + VenvFolder + " folder exists but has no interpreter. Delete that folder and run setup again to rebuild this copy's environment.");
            VerifyWheels();
            string runtime = Safe(EnvFolder + "/python/python.exe");
            if (!File.Exists(runtime)) throw new IOException("The vendored Python runtime is missing from " + EnvFolder + ".");
            int code = Run(runtime, "-m venv " + Quote(venv), 900);
            if (code != 0) throw new IOException("Creating " + VenvFolder + " failed (exit " + code + ").");
            created = true;
            code = Run(python, "-m pip install --disable-pip-version-check --no-index --no-deps"
                + " --find-links " + Quote(Safe(EnvFolder + "/wheels"))
                + " --requirement " + Quote(Safe(EnvFolder + "/requirements.txt")), 1800);
            if (code != 0) throw new IOException("Installing the vendored wheels failed (exit " + code + ").");
            if (Check(python) != 0) throw new IOException("The new " + VenvFolder + " cannot import the application.");
            ClearWarning();
            Log("environment ready: " + venv);
        } catch (Exception ex) {
            Log("environment failed: " + ex.Message);
            if (created) {
                try { DeleteTree(venv); } catch (Exception cleanup) { Log("could not remove the partial environment: " + cleanup.Message); }
            }
            Report(ex);
        }
    }

    /// <summary>The wheel set is verified before it is installed, never after.</summary>
    private static void VerifyWheels() {
        string list = Safe(EnvFolder + "/wheels.sha256");
        if (!File.Exists(list)) throw new IOException("The wheel checksum list " + EnvFolder + "/wheels.sha256 is missing.");
        int checkedFiles = 0;
        foreach (var raw in File.ReadAllLines(list)) {
            var line = raw.Trim();
            if (line.Length == 0) continue;
            int split = line.IndexOf("  ", StringComparison.Ordinal);
            if (split <= 0) throw new IOException("Malformed checksum line: " + line);
            string expected = line.Substring(0, split).ToLowerInvariant();
            string name = line.Substring(split + 2).Trim();
            string wheel = Safe(EnvFolder + "/wheels/" + name);
            if (!File.Exists(wheel)) throw new IOException("A vendored wheel is missing: " + name);
            string actual;
            using (var sha = SHA256.Create())
            using (var stream = File.OpenRead(wheel))
                actual = BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
            if (actual != expected) throw new IOException("A vendored wheel does not match its checksum: " + name);
            checkedFiles++;
        }
        if (checkedFiles == 0) throw new IOException("The wheel checksum list is empty.");
        Log("environment: verified " + checkedFiles + " wheels");
    }

    private static int Check(string python) {
        return Run(python, "-c \"import boardmodeler, numpy, pydantic, pypdf, pypdfium2;"
            + " print('boardmodeler', boardmodeler.__version__)\"", 300);
    }

    private static string Quote(string value) {
        return "\"" + value.TrimEnd(Path.DirectorySeparatorChar) + "\"";
    }

    /// <summary>
    /// Run a child with a cleaned environment: a caller's PYTHONHOME/PYTHONPATH/virtualenv
    /// variables must not reach the environment this copy builds for itself.
    /// </summary>
    private static int Run(string file, string arguments, int timeoutSeconds) {
        var info = new ProcessStartInfo(file, arguments) {
            WorkingDirectory = Root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (string name in new[] { "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE", "VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PIP_REQUIRE_VIRTUALENV" })
            info.EnvironmentVariables.Remove(name);
        info.EnvironmentVariables["PYTHONNOUSERSITE"] = "1";
        info.EnvironmentVariables["PIP_DISABLE_PIP_VERSION_CHECK"] = "1";
        info.EnvironmentVariables["PIP_NO_INPUT"] = "1";
        info.EnvironmentVariables["SPICE_MAKER_ROOT"] = Root;
        var output = new StringBuilder();
        using (var process = new Process()) {
            process.StartInfo = info;
            process.OutputDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) lock (output) output.AppendLine(e.Data); };
            process.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) lock (output) output.AppendLine(e.Data); };
            Log("> " + Path.GetFileName(file) + " " + arguments);
            process.Start();
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            if (!process.WaitForExit(timeoutSeconds * 1000)) {
                try { process.Kill(); } catch (Exception) { }
                Log("  timed out after " + timeoutSeconds + "s");
                Log(Indented(output.ToString()));
                return -1;
            }
            process.WaitForExit();
            int code = process.ExitCode;
            Log("  exit " + code);
            Log(Indented(output.ToString()));
            return code;
        }
    }

    private static string Indented(string text) {
        return "    " + text.TrimEnd().Replace("\r\n", "\r\n    ").Replace("\n", "\r\n    ");
    }

    private static void Report(Exception ex) {
        string message = "This copy runs, but its own Python environment in " + VenvFolder
            + " could not be created, so Boardmodeler.cmd needs it rebuilt.\r\n"
            + "The frozen app in " + AppFolder + " is unaffected.\r\n\r\n"
            + "Reason: " + ex.Message + "\r\n\r\nDetails: " + SetupLog + " and " + SetupError + " in "
            + Root + ". Delete " + VenvFolder + " and run Install.exe again to retry.";
        try { File.WriteAllText(Safe(SetupError), message); } catch (Exception) { }
        EnvironmentWarning = message;
    }

    private static void ClearWarning() {
        try { File.Delete(Safe(SetupError)); } catch (Exception) { }
        EnvironmentWarning = null;
    }

    private static void Fail(Exception ex) {
        Result = 1;
        Log("failed: " + ex);
        string message = "Setup could not finish. Close this app and use a writable extracted folder.\n\n" + ex.Message;
        try { File.WriteAllText(Safe("install-error.txt"), message); } catch {}
        if (!Silent) MessageBox.Show(message, Edition + " setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
