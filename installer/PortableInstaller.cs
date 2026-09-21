using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading.Tasks;
using System.Windows.Forms;

[assembly: System.Runtime.Versioning.TargetFramework(".NETFramework,Version=v4.8")]
[assembly: AssemblyVersion("1.3.0.0")]
[assembly: AssemblyFileVersion("1.3.0.0")]

internal static class PortableInstaller {
    private static string Root;
    private static string Edition;
    private static int Result;
    private static bool Silent;

    [STAThread]
    private static int Main(string[] args) {
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        Root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        Silent = Array.IndexOf(args, "--silent") >= 0;
        bool launch = Array.IndexOf(args, "--no-launch") < 0;
        using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("edition.txt"))
        using (var reader = new StreamReader(stream)) { Edition = reader.ReadToEnd().Trim(); }
        Application.EnableVisualStyles();
        if (Silent) {
            try { Install(); } catch (Exception ex) { Fail(ex); }
        } else {
            using (var form = new Form())
            using (var gif = Assembly.GetExecutingAssembly().GetManifestResourceStream("splash.gif")) {
                form.Text = Edition + " 1.3.0 setup";
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
        }
        if (Result == 0 && launch) {
            try {
                Process.Start(new ProcessStartInfo(Safe("app/SpiceMaker.exe")) {
                    WorkingDirectory = Root, UseShellExecute = false
                });
            } catch (Exception ex) { Fail(ex); }
        }
        return Result;
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

    private static void Install() {
        var marker = Safe(".spice-maker-root");
        if (File.Exists(marker) && File.ReadAllText(marker).Trim() != Edition)
            throw new IOException("This folder belongs to a different edition. Extract each edition into its own folder.");
        string staging = Safe(".install-" + Guid.NewGuid().ToString("N"));
        string app = Safe("app");
        string backup = Safe(".previous-" + Guid.NewGuid().ToString("N"));
        bool moved = false;
        using (var installLock = new FileStream(Safe(".install.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None)) {
            try {
                Directory.CreateDirectory(staging);
                using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
                using (var archive = new ZipArchive(stream, ZipArchiveMode.Read)) {
                    foreach (var entry in archive.Entries) {
                        var path = Path.GetFullPath(Path.Combine(staging, entry.FullName));
                        if (!path.StartsWith(staging + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                            throw new IOException("Invalid archive path.");
                        if (String.IsNullOrEmpty(entry.Name)) { Directory.CreateDirectory(path); continue; }
                        Directory.CreateDirectory(Path.GetDirectoryName(path));
                        using (var input = entry.Open())
                        using (var output = File.Create(path)) input.CopyTo(output);
                    }
                }
                if (!File.Exists(Path.Combine(staging, "SpiceMaker.exe"))) throw new IOException("The app is missing from the payload.");
                if (Directory.Exists(app)) {
                    if ((File.GetAttributes(app) & FileAttributes.ReparsePoint) != 0) throw new IOException("The app folder must not be a link.");
                    // Detect a running app before touching any installed files.
                    foreach (var file in Directory.GetFiles(app, "*.exe", SearchOption.TopDirectoryOnly))
                        using (File.Open(file, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) {}
                    Directory.Move(app, backup); moved = true;
                }
                Directory.Move(staging, app);
                File.WriteAllText(marker, Edition);
                File.WriteAllText(Safe("Start.cmd"), "@echo off\r\nstart \"\" /D \"%~dp0\" \"%~dp0app\\SpiceMaker.exe\"\r\n");
                if (moved) DeleteTree(backup);
            } catch {
                if (moved && !Directory.Exists(app)) Directory.Move(backup, app);
                throw;
            } finally { if (Directory.Exists(staging)) DeleteTree(staging); }
        }
        File.Delete(Safe(".install.lock"));
    }

    private static void Fail(Exception ex) {
        Result = 1;
        string message = "Setup could not finish. Close this app and use a writable extracted folder.\n\n" + ex.Message;
        try { File.WriteAllText(Safe("install-error.txt"), message); } catch {}
        if (!Silent) MessageBox.Show(message, "Spice Maker setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}
