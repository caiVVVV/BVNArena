// BVN Arena process-tree audio capture. Build with the Windows .NET Framework csc.
// Each stdout record is <uint64 QPC in 100 ns, uint32 sample frames, uint32 flags>
// followed by little-endian 44.1 kHz signed 16-bit stereo PCM.
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

[ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IActivation {
    [PreserveSig] int GetActivateResult(out int hr, [MarshalAs(UnmanagedType.IUnknown)] out object audio);
}
[ComVisible(true), Guid("41D949AB-9862-444A-80F6-C261334DA5EB"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface ICompletion {
    [PreserveSig] int ActivateCompleted(IntPtr operation);
}
[ComVisible(true), Guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IAgile { }
[ComVisible(true), ClassInterface(ClassInterfaceType.None)]
public class Completion : ICompletion, IAgile {
    public ManualResetEvent Ready = new ManualResetEvent(false);
    public int Status;
    public object Instance;
    public int ActivateCompleted(IntPtr operation) {
        try {
            var activation = (IActivation)Marshal.GetObjectForIUnknown(operation);
            int hr = activation.GetActivateResult(out Status, out Instance);
            if (hr < 0) Status = hr;
        } catch (Exception ex) { Status = ex.HResult; }
        finally { Ready.Set(); }
        return 0;
    }
}
[StructLayout(LayoutKind.Sequential, Pack=2)]
struct WaveFormat {
    public ushort tag, channels; public uint rate, bytesPerSecond; public ushort alignment, bits, extra;
}
[StructLayout(LayoutKind.Sequential)]
struct BlobVariant {
    public ushort vt, a, b, c; public uint length; public IntPtr data;
}
[ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioClient {
    [PreserveSig] int Initialize(int mode, uint flags, long duration, long period, ref WaveFormat format, IntPtr session);
    [PreserveSig] int GetBufferSize(out uint size);
    [PreserveSig] int GetStreamLatency(out long latency);
    [PreserveSig] int GetCurrentPadding(out uint padding);
    [PreserveSig] int IsFormatSupported(int mode, IntPtr format, IntPtr closest);
    [PreserveSig] int GetMixFormat(out IntPtr format);
    [PreserveSig] int GetDevicePeriod(out long defaultPeriod, out long minimum);
    [PreserveSig] int Start();
    [PreserveSig] int Stop();
    [PreserveSig] int Reset();
    [PreserveSig] int SetEventHandle(IntPtr handle);
    [PreserveSig] int GetService(ref Guid id, [MarshalAs(UnmanagedType.IUnknown)]out object service);
}
[ComImport, Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioCapture {
    [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpc);
    [PreserveSig] int ReleaseBuffer(uint frames);
    [PreserveSig] int GetNextPacketSize(out uint frames);
}
class ProcessAudio {
    [DllImport("Mmdevapi.dll", CharSet=CharSet.Unicode, ExactSpelling=true)]
    static extern int ActivateAudioInterfaceAsync(string device, ref Guid iid, ref BlobVariant variant,
        [MarshalAs(UnmanagedType.Interface)] ICompletion completion, out IntPtr operation);
    static void Check(int status) { if (status < 0) Marshal.ThrowExceptionForHR(status); }
    [MTAThread]
    static int Main(string[] args) {
        try {
            int target = int.Parse(args[0]);
            using (var parent = Process.GetProcessById(int.Parse(args[1]))) {
                Run(target, parent);
            }
            return 0;
        } catch (Exception ex) {
            Console.Error.WriteLine("0x" + ex.HResult.ToString("X8") + " " + ex.Message);
            return 1;
        }
    }
    static void Run(int target, Process parent) {
        IntPtr parameters = Marshal.AllocCoTaskMem(12), operation = IntPtr.Zero;
        var complete = new Completion();
        try {
            Marshal.Copy(new int[] {1, target, 0}, 0, parameters, 3);
            var variant = new BlobVariant {vt=65, length=12, data=parameters};
            Guid iid = typeof(IAudioClient).GUID;
            Check(ActivateAudioInterfaceAsync(@"VAD\Process_Loopback", ref iid, ref variant, complete, out operation));
            if (!complete.Ready.WaitOne(5000)) throw new TimeoutException("process audio activation");
            Check(complete.Status);
            var client = (IAudioClient)complete.Instance;
            var format = new WaveFormat {tag=1, channels=2, rate=44100, bytesPerSecond=176400, alignment=4, bits=16};
            Check(client.Initialize(0, 0x80060000, 0, 0, ref format, IntPtr.Zero));
            Guid captureId = typeof(IAudioCapture).GUID;
            object service;
            Check(client.GetService(ref captureId, out service));
            var capture = (IAudioCapture)service;
            using (var signal = new AutoResetEvent(false))
            using (var output = new BinaryWriter(Console.OpenStandardOutput())) {
                Check(client.SetEventHandle(signal.SafeWaitHandle.DangerousGetHandle()));
                Check(client.Start());
                byte[] samples = new byte[0];
                output.Write((ulong)0); output.Write((uint)0); output.Write((uint)0); output.Flush();
                while (!parent.HasExited) {
                    if (!signal.WaitOne(200)) continue;
                    uint available;
                    Check(capture.GetNextPacketSize(out available));
                    while (available > 0) {
                        IntPtr data; uint frames, flags; ulong position, qpc;
                        Check(capture.GetBuffer(out data, out frames, out flags, out position, out qpc));
                        int length = checked((int)frames * 4);
                        if (samples.Length < length) samples = new byte[length];
                        if ((flags & 2) != 0) Array.Clear(samples, 0, length);
                        else Marshal.Copy(data, samples, 0, length);
                        Check(capture.ReleaseBuffer(frames));
                        output.Write(qpc); output.Write(frames); output.Write(flags); output.Write(samples, 0, length); output.Flush();
                        Check(capture.GetNextPacketSize(out available));
                    }
                }
                client.Stop();
            }
            GC.KeepAlive(complete);
        } finally {
            if (operation != IntPtr.Zero) Marshal.Release(operation);
            Marshal.FreeCoTaskMem(parameters);
        }
    }
}
