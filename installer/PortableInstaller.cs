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
    private static volatile string SetupStage = "Preparing the application";

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
            using (var form = CreateSetupForm()) {
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

    /// <summary>A compact classic setup screen; no estimated percentage is invented.</summary>
    private static Form CreateSetupForm() {
        var form = new Form {
            Text = Edition + " " + Version + " setup",
            FormBorderStyle = FormBorderStyle.FixedDialog,
            MaximizeBox = false, MinimizeBox = false,
            StartPosition = FormStartPosition.CenterScreen,
            BackColor = Color.FromArgb(212, 208, 200),
            Font = new Font("Segoe UI", 9f),
            AutoSize = true, AutoSizeMode = AutoSizeMode.GrowAndShrink,
            Padding = new Padding(14)
        };
        var layout = new TableLayoutPanel {
            AutoSize = true, Dock = DockStyle.Fill, ColumnCount = 1,
            Margin = Padding.Empty, Padding = Padding.Empty
        };
        layout.Controls.Add(new SetupPepper {
            Width = 440, Height = 120, Dock = DockStyle.Fill,
            Margin = new Padding(0, 0, 0, 12)
        });
        var stage = new Label {
            Text = SetupStage, AutoSize = true, MaximumSize = new Size(440, 0),
            Margin = new Padding(0, 0, 0, 9)
        };
        layout.Controls.Add(stage);
        var progress = new SetupActivityBar {
            Width = 440, Height = 26, Dock = DockStyle.Fill,
            Margin = new Padding(0, 0, 0, 9)
        };
        layout.Controls.Add(progress);
        var elapsed = new Label {
            Text = "ELAPSED 00:00:00", AutoSize = true, ForeColor = Color.Navy,
            Font = new Font("Consolas", 9f), Margin = new Padding(0, 0, 0, 10)
        };
        layout.Controls.Add(elapsed);
        layout.Controls.Add(new Label {
            Text = "Installing into this folder. Your saved models and settings stay here.",
            AutoSize = true, MaximumSize = new Size(440, 0), Margin = Padding.Empty
        });
        form.Controls.Add(layout);
        var clock = Stopwatch.StartNew();
        var timer = new System.Windows.Forms.Timer { Interval = 120 };
        timer.Tick += (s,e) => {
            stage.Text = SetupStage;
            elapsed.Text = "ELAPSED " + clock.Elapsed.ToString(@"hh\:mm\:ss");
            progress.Advance();
        };
        form.Shown += (s,e) => timer.Start();
        form.Disposed += (s,e) => timer.Dispose();
        return form;
    }

    /// <summary>Square-pixel brand animation, independent of installation progress.</summary>
    private sealed class SetupPepper : Control {
        private readonly MemoryStream imageData = new MemoryStream();
        private readonly Image image;
        private readonly System.Windows.Forms.Timer timer;
        private readonly int frames;
        private int frame;

        public SetupPepper() {
            DoubleBuffered = true;
            AccessibleName = "Spice Maker setup";
            using (var input = Assembly.GetExecutingAssembly().GetManifestResourceStream("pepper-splash.gif")) {
                if (input == null) throw new InvalidDataException("The setup animation is missing.");
                input.CopyTo(imageData);
            }
            imageData.Position = 0;
            image = Image.FromStream(imageData);
            frames = image.GetFrameCount(System.Drawing.Imaging.FrameDimension.Time);
            timer = new System.Windows.Forms.Timer { Interval = 40 };
            timer.Tick += (s,e) => {
                frame = (frame + 1) % frames;
                image.SelectActiveFrame(System.Drawing.Imaging.FrameDimension.Time, frame);
                Invalidate();
            };
        }

        protected override void OnHandleCreated(EventArgs e) {
            base.OnHandleCreated(e);
            timer.Start();
        }

        protected override void OnHandleDestroyed(EventArgs e) {
            timer.Stop();
            base.OnHandleDestroyed(e);
        }

        protected override void OnPaint(PaintEventArgs e) {
            e.Graphics.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.NearestNeighbor;
            e.Graphics.PixelOffsetMode = System.Drawing.Drawing2D.PixelOffsetMode.Half;
            e.Graphics.DrawImage(image, ClientRectangle, 0, 0, image.Width, image.Height, GraphicsUnit.Pixel);
        }

        protected override void Dispose(bool disposing) {
            if (disposing) {
                timer.Dispose();
                image.Dispose();
                imageData.Dispose();
            }
            base.Dispose(disposing);
        }
    }

    /// <summary>Navy moving blocks show activity, without claiming percent complete.</summary>
    private sealed class SetupActivityBar : Control {
        private int step;
        public SetupActivityBar() {
            DoubleBuffered = true;
            AccessibleName = "Setup is working; progress is not a percentage";
        }
        public void Advance() { step = (step + 1) % 24; Invalidate(); }
        protected override void OnPaint(PaintEventArgs e) {
            e.Graphics.Clear(Color.White);
            ControlPaint.DrawBorder3D(e.Graphics, ClientRectangle, Border3DStyle.Sunken);
            int block = 14, count = Math.Max(1, (Width - 4) / block);
            using (var brush = new SolidBrush(Color.Navy)) {
                for (int index = 0; index < count; index++) {
                    if ((index - step + 48) % 24 < 8)
                        e.Graphics.FillRectangle(brush, 3 + index * block, 3, block - 2, Height - 6);
                }
            }
        }
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
        SetupStage = "Unpacking the application and local tools";
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
        SetupStage = "Updating this copy of Spice Maker";
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
    /// Keep a matching environment unchanged; replace stale packages from verified local
    /// wheels. The interpreter and user data stay in place, with package rollback on failure.
    /// </summary>
    private static void ProvisionEnvironment() {
        SetupStage = "Preparing the local command-line tools";
        string venv = Safe(VenvFolder);
        string python = Safe(VenvFolder + "/Scripts/python.exe");
        bool created = false;
        try {
            RefuseLinkedAncestors(venv);
            ExpectedEngineSnapshot();
            if (File.Exists(python)) {
                if (Check(python) == 0) {
                    Log("environment: existing " + VenvFolder + " matches the packaged engine");
                    ClearWarning(); return;
                }
                Log("environment: upgrading stale packages in the existing " + VenvFolder);
                UpgradePackages(python);
                ClearWarning();
                return;
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
            if (Check(python) != 0) throw new IOException("The new " + VenvFolder + " does not match the packaged engine.");
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

    private static string ExpectedEngineSnapshot() {
        string snapshot = Safe(AppFolder + "/_internal/boardmodeler/engine-identity.json");
        if (!File.Exists(snapshot))
            throw new IOException("The packaged engine identity is missing; this installer cannot verify its command-line environment.");
        return snapshot;
    }

    /// <summary>Stage only local wheel packages, then activate them with a rollback copy.</summary>
    private static void UpgradePackages(string python) {
        VerifyWheels();
        string packages = Safe(VenvFolder + "/Lib/site-packages");
        RefuseLinkedAncestors(packages);
        if (!Directory.Exists(packages))
            throw new IOException("The existing environment has no site-packages directory.");
        string staging = Safe(".venv-upgrade-" + Guid.NewGuid().ToString("N"));
        string candidate = Path.Combine(staging, "site-packages");
        string backup = Safe(".venv-packages-previous-" + Guid.NewGuid().ToString("N"));
        try {
            CopyTree(packages, candidate);
            RemoveReplacedMetadata(candidate);
            int code = Run(python, "-m pip install --disable-pip-version-check --no-index --no-deps"
                + " --upgrade --force-reinstall --target " + Quote(candidate)
                + " --find-links " + Quote(Safe(EnvFolder + "/wheels"))
                + " --requirement " + Quote(Safe(EnvFolder + "/requirements.txt")), 1800);
            if (code != 0) throw new IOException("Upgrading the vendored packages failed (exit " + code + "). The original environment was kept.");
            if (Check(python, candidate) != 0)
                throw new IOException("The staged packages do not match the packaged engine. The original environment was kept.");
            Directory.Move(packages, backup);
            try {
                Directory.Move(candidate, packages);
                if (Check(python) != 0)
                    throw new IOException("The upgraded environment did not match the packaged engine.");
            } catch {
                if (Directory.Exists(packages)) DeleteTree(packages);
                Directory.Move(backup, packages);
                Log("environment: restored the previous packages after a failed upgrade");
                throw;
            }
            DeleteTree(backup);
            Log("environment: upgraded local packages to " + Version + " with the packaged engine identity");
        } finally {
            if (Directory.Exists(staging)) DeleteTree(staging);
        }
    }

    private static void CopyTree(string source, string destination) {
        source = Safe(source);
        destination = Safe(destination);
        if ((File.GetAttributes(source) & FileAttributes.ReparsePoint) != 0)
            throw new IOException("Refusing to upgrade a linked package directory.");
        Directory.CreateDirectory(destination);
        foreach (string file in Directory.GetFiles(source)) {
            if ((File.GetAttributes(file) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Refusing to copy a linked package file.");
            File.Copy(Safe(file), Safe(Path.Combine(destination, Path.GetFileName(file))));
        }
        foreach (string child in Directory.GetDirectories(source))
            CopyTree(child, Path.Combine(destination, Path.GetFileName(child)));
    }

    private static void RefuseLinkedAncestors(string path) {
        string current = Safe(path);
        while (!String.Equals(current.TrimEnd(Path.DirectorySeparatorChar), Root.TrimEnd(Path.DirectorySeparatorChar), StringComparison.OrdinalIgnoreCase)) {
            if ((Directory.Exists(current) || File.Exists(current))
                && (File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Refusing to upgrade an environment through a linked path: " + current);
            current = Path.GetDirectoryName(current);
        }
    }

    private static void RemoveReplacedMetadata(string packages) {
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (string wheel in Directory.GetFiles(Safe(EnvFolder + "/wheels"), "*.whl"))
            names.Add(Path.GetFileName(wheel).Split('-')[0].Replace('_', '-'));
        foreach (string metadata in Directory.GetDirectories(Safe(packages), "*.dist-info")) {
            string name = Path.GetFileName(metadata).Split('-')[0].Replace('_', '-');
            if (names.Contains(name)) DeleteTree(metadata);
        }
    }

    /// <summary>The wheel set is verified before it is installed, never after.</summary>
    private static void VerifyWheels() {
        string list = Safe(EnvFolder + "/wheels.sha256");
        if (!File.Exists(list)) throw new IOException("The wheel checksum list " + EnvFolder + "/wheels.sha256 is missing.");
        int checkedFiles = 0;
        var verified = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
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
            verified.Add(wheel);
            checkedFiles++;
        }
        if (checkedFiles == 0) throw new IOException("The wheel checksum list is empty.");
        foreach (string wheel in Directory.GetFiles(Safe(EnvFolder + "/wheels"), "*.whl"))
            if (!verified.Contains(Safe(wheel)))
                throw new IOException("An unverified wheel is present: " + Path.GetFileName(wheel));
        Log("environment: verified " + checkedFiles + " wheels");
    }

    private static int Check(string python) {
        return Check(python, "");
    }

    private static int Check(string python, string packageRoot) {
        return Run(python, "-c \"import sys,json;"
            + " sys.path.insert(0,sys.argv[3]) if sys.argv[3] else None;"
            + " import boardmodeler,numpy,pydantic,pypdf,pypdfium2;"
            + " from boardmodeler.engine_identity import engine_contract;"
            + " expected=json.load(open(sys.argv[1],encoding='utf-8'));"
            + " assert boardmodeler.__version__==sys.argv[2], 'packaged_version_mismatch';"
            + " assert engine_contract()==expected, 'packaged_engine_mismatch';"
            + " print('boardmodeler',boardmodeler.__version__,expected['source_sha256'])\" "
            + Quote(ExpectedEngineSnapshot()) + " " + Quote(Version) + " " + Quote(packageRoot), 300);
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
            + " could not be prepared or upgraded, so Boardmodeler.cmd may still use an older engine.\r\n"
            + "The frozen app in " + AppFolder + " is unaffected.\r\n\r\n"
            + "Reason: " + ex.Message + "\r\n\r\nDetails: " + SetupLog + " and " + SetupError + " in "
            + Root + ". Run Install.exe again to retry; the previous environment is retained when an upgrade fails.";
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
