// Microphone test helper for the AndroUnderTheHood "Microphone test" plugin.
// Runs on the device with:  CLASSPATH=mictest.jar app_process / MicTest list|stream ...
//
//   list                                              -> JSON: input devices, physical microphones, policy ports
//   stream <deviceId|-> <source> <rate> <channels> [token]
//        channels: 1, 2, or N>2 = index mask with N channels (one per capsule on many HALs)
//        -> packets on stdout: [type u8][len u32 BE][payload]
//           'I' info JSON, 'A' PCM s16le interleaved, 'S' status JSON (about once a second), 'E' error text
//
// Built against android.jar with javac --release 11 and d8 (see build_helper.sh).

import android.annotation.SuppressLint;
import android.app.Application;
import android.app.Instrumentation;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioRecordingConfiguration;
import android.media.MicrophoneInfo;
import android.os.Build;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.util.Pair;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedOutputStream;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.List;

@SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "SoonBlockedPrivateApi", "MissingPermission", "NewApi",
        "WrongConstant"})
public final class MicTest {
    static final String PKG = "com.android.shell";
    static Context ctx;

    // ------------------------------------------------------------------ fake context (as in scrcpy)
    static final class FakeContext extends ContextWrapper {
        FakeContext(Context base) { super(base); }
        @Override public String getPackageName() { return PKG; }
        @Override public String getOpPackageName() { return PKG; }
        @Override public Context getApplicationContext() { return this; }
        @Override public AttributionSource getAttributionSource() {
            AttributionSource.Builder b = new AttributionSource.Builder(Process.myUid());
            b.setPackageName(PKG);
            return b.build();
        }
        @SuppressWarnings("unused") public int getDeviceId() { return 0; }
    }

    static Context makeContext() throws Exception {
        Class<?> at = Class.forName("android.app.ActivityThread");
        Constructor<?> c = at.getDeclaredConstructor();
        c.setAccessible(true);
        Object thread = c.newInstance();
        setField(at, null, "sCurrentActivityThread", thread);
        try { setField(at, thread, "mSystemThread", true); } catch (Throwable ignored) { }
        if (Build.VERSION.SDK_INT >= 31) {
            try {
                Class<?> cc = Class.forName("android.app.ConfigurationController");
                Class<?> ati = Class.forName("android.app.ActivityThreadInternal");
                Constructor<?> k = cc.getDeclaredConstructor(ati);
                k.setAccessible(true);
                setField(at, thread, "mConfigurationController", k.newInstance(thread));
            } catch (Throwable ignored) { }
        }
        try {
            Class<?> abd = Class.forName("android.app.ActivityThread$AppBindData");
            Constructor<?> k = abd.getDeclaredConstructor();
            k.setAccessible(true);
            Object data = k.newInstance();
            ApplicationInfo ai = new ApplicationInfo();
            ai.packageName = PKG;
            setField(abd, data, "appInfo", ai);
            setField(at, thread, "mBoundApplication", data);
        } catch (Throwable ignored) { }
        Method m = at.getDeclaredMethod("getSystemContext");
        m.setAccessible(true);
        FakeContext fc = new FakeContext((Context) m.invoke(thread));
        try {
            Application app = Instrumentation.newApplication(Application.class, fc);
            setField(at, thread, "mInitialApplication", app);
        } catch (Throwable ignored) { }
        return fc;
    }

    static void setField(Class<?> cls, Object obj, String name, Object value) throws Exception {
        Field f = cls.getDeclaredField(name);
        f.setAccessible(true);
        f.set(obj, value);
    }

    static AudioManager audio() {
        return (AudioManager) ctx.getSystemService(Context.AUDIO_SERVICE);
    }

    public static void main(String[] args) {
        try {
            if (Looper.getMainLooper() == null) Looper.prepareMainLooper();
        } catch (Throwable ignored) { }
        try {
            ctx = makeContext();
            if (args.length >= 1 && args[0].equals("list")) {
                System.out.println(list().toString());
                System.out.flush();
                System.exit(0);
            } else if (args.length >= 5 && args[0].equals("stream")) {
                stream(args[1].equals("-") ? 0 : Integer.parseInt(args[1]), Integer.parseInt(args[2]),
                        Integer.parseInt(args[3]), Integer.parseInt(args[4]));
            } else {
                System.err.println("usage: MicTest list | stream <deviceId|-> <source> <rate> <channels>");
                System.exit(2);
            }
        } catch (Throwable t) {
            t.printStackTrace();
            System.exit(1);
        }
    }

    // ------------------------------------------------------------------ names
    static String typeName(int t) {
        switch (t) {
            case 1: return "Built-in earpiece";
            case 2: return "Built-in speaker";
            case 3: return "Wired headset";
            case 4: return "Wired headphones";
            case 5: return "Line analog";
            case 6: return "Line digital";
            case 7: return "Bluetooth SCO";
            case 8: return "Bluetooth A2DP";
            case 9: return "HDMI";
            case 10: return "HDMI ARC";
            case 11: return "USB device";
            case 12: return "USB accessory";
            case 13: return "Dock";
            case 14: return "FM";
            case 15: return "Built-in mic";
            case 16: return "FM tuner";
            case 17: return "TV tuner";
            case 18: return "Telephony";
            case 19: return "AUX line";
            case 20: return "IP";
            case 21: return "Bus";
            case 22: return "USB headset";
            case 23: return "Hearing aid";
            case 24: return "Built-in speaker (safe)";
            case 25: return "Remote submix";
            case 26: return "BLE headset";
            case 27: return "BLE speaker";
            case 28: return "Echo reference";
            case 29: return "HDMI eARC";
            case 30: return "BLE broadcast";
            case 31: return "Dock analog";
            default: return "Type " + t;
        }
    }

    static boolean isMicType(int t) {
        return t == 15 || t == 3 || t == 7 || t == 11 || t == 22 || t == 12 || t == 26 || t == 5 || t == 6
                || t == 19 || t == 23 || t == 13 || t == 31;
    }

    static String locationName(int l) {
        switch (l) {
            case 1: return "main body";
            case 2: return "main body (movable)";
            case 3: return "peripheral";
            default: return "unknown";
        }
    }

    static String directionName(int d) {
        switch (d) {
            case 1: return "omni";
            case 2: return "bi-directional";
            case 3: return "cardioid";
            case 4: return "hyper-cardioid";
            case 5: return "super-cardioid";
            default: return "unknown";
        }
    }

    static JSONObject mic(MicrophoneInfo m) throws Exception {
        JSONObject o = new JSONObject();
        o.put("description", m.getDescription());
        o.put("mic_id", m.getId());
        o.put("type", m.getType());
        o.put("type_name", typeName(m.getType()));
        o.put("address", m.getAddress());
        o.put("location", locationName(m.getLocation()));
        o.put("group", m.getGroup());
        o.put("index_in_group", m.getIndexInTheGroup());
        o.put("directionality", directionName(m.getDirectionality()));
        MicrophoneInfo.Coordinate3F p = m.getPosition();
        if (p != null && p.x != MicrophoneInfo.POSITION_UNKNOWN.x)
            o.put("position_m", String.format("%.3f, %.3f, %.3f", p.x, p.y, p.z));
        MicrophoneInfo.Coordinate3F d = m.getOrientation();
        if (d != null && d.x != MicrophoneInfo.ORIENTATION_UNKNOWN.x)
            o.put("orientation", String.format("%.2f, %.2f, %.2f", d.x, d.y, d.z));
        if (m.getSensitivity() != MicrophoneInfo.SENSITIVITY_UNKNOWN) o.put("sensitivity_dbfs", m.getSensitivity());
        if (m.getMaxSpl() != MicrophoneInfo.SPL_UNKNOWN) o.put("max_spl", m.getMaxSpl());
        if (m.getMinSpl() != MicrophoneInfo.SPL_UNKNOWN) o.put("min_spl", m.getMinSpl());
        JSONArray ch = new JSONArray();
        try {
            for (Pair<Integer, Integer> c : m.getChannelMapping()) ch.put(c.first + ":" + (c.second == 1 ? "direct" : c.second == 2 ? "processed" : "unused"));
        } catch (Throwable ignored) { }
        if (ch.length() > 0) o.put("channel_mapping", ch);
        return o;
    }

    // ------------------------------------------------------------------ list
    static JSONObject list() throws Exception {
        JSONObject root = new JSONObject();
        root.put("sdk", Build.VERSION.SDK_INT);
        root.put("model", Build.MANUFACTURER + " " + Build.MODEL);
        root.put("uid", Process.myUid());
        AudioManager am = audio();

        JSONArray devs = new JSONArray();
        try {
            for (AudioDeviceInfo d : am.getDevices(AudioManager.GET_DEVICES_INPUTS)) {
                JSONObject o = new JSONObject();
                o.put("id", d.getId());
                o.put("type", d.getType());
                o.put("type_name", typeName(d.getType()));
                o.put("name", String.valueOf(d.getProductName()));
                if (Build.VERSION.SDK_INT >= 28) o.put("address", d.getAddress());
                o.put("is_mic", isMicType(d.getType()));
                JSONArray cc = new JSONArray();
                for (int c : d.getChannelCounts()) cc.put(c);
                o.put("channel_counts", cc);
                JSONArray im = new JSONArray();
                for (int c : d.getChannelIndexMasks()) im.put(Integer.bitCount(c));
                o.put("index_channels", im);
                JSONArray sr = new JSONArray();
                for (int r : d.getSampleRates()) sr.put(r);
                o.put("sample_rates", sr);
                devs.put(o);
            }
        } catch (Throwable t) {
            root.put("devices_error", String.valueOf(t));
        }
        root.put("devices", devs);

        JSONArray mics = new JSONArray();
        if (Build.VERSION.SDK_INT >= 28) {
            try {
                for (MicrophoneInfo m : am.getMicrophones()) mics.put(mic(m));
            } catch (Throwable t) {
                root.put("microphones_error", String.valueOf(t));
            }
        }
        root.put("microphones", mics);
        try {
            root.put("unprocessed_supported",
                    "true".equals(am.getProperty(AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED)));
            root.put("native_rate", am.getProperty(AudioManager.PROPERTY_OUTPUT_SAMPLE_RATE));
        } catch (Throwable ignored) { }
        return root;
    }

    // ------------------------------------------------------------------ stream
    static OutputStream out;

    static synchronized void packet(char type, byte[] data, int len) throws IOException {
        out.write(new byte[]{(byte) type, (byte) (len >>> 24), (byte) (len >>> 16), (byte) (len >>> 8), (byte) len});
        out.write(data, 0, len);
        out.flush();
    }

    static void packet(char type, JSONObject o) throws IOException {
        byte[] b = o.toString().getBytes("UTF-8");
        packet(type, b, b.length);
    }

    static void fail(String msg) {
        try {
            byte[] b = msg.getBytes("UTF-8");
            packet('E', b, b.length);
        } catch (Throwable ignored) { }
        System.err.println(msg);
        System.exit(1);
    }

    static AudioRecord build(int source, AudioFormat fmt, int bufBytes) {
        try {
            AudioRecord.Builder b = new AudioRecord.Builder();
            if (Build.VERSION.SDK_INT >= 31) b.setContext(ctx);
            b.setAudioSource(source).setAudioFormat(fmt).setBufferSizeInBytes(bufBytes);
            return b.build();
        } catch (UnsupportedOperationException e) {
            throw e;
        } catch (RuntimeException e) {     // some vendor builds break the Builder; plain constructor as fallback
            if (fmt.getChannelMask() == 0) throw e;
            return new AudioRecord(source, fmt.getSampleRate(), fmt.getChannelMask(), fmt.getEncoding(), bufBytes);
        }
    }

    static void stream(int deviceId, int source, int rate, int channels) throws Exception {
        out = new BufferedOutputStream(new FileOutputStream(FileDescriptor.out), 1 << 15);
        AudioManager am = audio();
        AudioDeviceInfo dev = null;
        if (deviceId != 0) {
            for (AudioDeviceInfo d : am.getDevices(AudioManager.GET_DEVICES_INPUTS)) if (d.getId() == deviceId) dev = d;
            if (dev == null) fail("input device " + deviceId + " is gone (unplugged?)");
        }
        AudioFormat.Builder fb = new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(rate);
        if (channels == 1) fb.setChannelMask(AudioFormat.CHANNEL_IN_MONO);
        else if (channels == 2) fb.setChannelMask(AudioFormat.CHANNEL_IN_STEREO);
        else fb.setChannelIndexMask((1 << channels) - 1);
        AudioFormat fmt = fb.build();
        int min = AudioRecord.getMinBufferSize(rate, channels == 2 ? AudioFormat.CHANNEL_IN_STEREO : AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        int buf = Math.max(min > 0 ? min * 4 : 0, rate * channels * 2 / 5);   // >= 200 ms

        // Android 11 only lets the foreground uid record: briefly bring a shell activity to the front (as scrcpy does).
        boolean a11 = Build.VERSION.SDK_INT == 30 && Process.myUid() == 2000;
        if (a11) {
            try {
                Runtime.getRuntime().exec(new String[]{"am", "start", "-n", "com.android.shell/.HeapDumpActivity"}).waitFor();
            } catch (Throwable ignored) { }
        }
        AudioRecord rec = null;
        String err = null;
        for (int attempt = 0; attempt < (a11 ? 6 : 1) && rec == null; attempt++) {
            if (a11) SystemClock.sleep(150);
            try {
                AudioRecord r = build(source, fmt, buf);
                if (r.getState() != AudioRecord.STATE_INITIALIZED) {
                    r.release();
                    throw new IllegalStateException("AudioRecord did not initialise (format not supported?)");
                }
                if (dev != null) r.setPreferredDevice(dev);
                r.startRecording();
                if (r.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) {
                    r.release();
                    throw new IllegalStateException("recording did not start (microphone in use or blocked)");
                }
                rec = r;
            } catch (Throwable t) {
                err = t.getClass().getSimpleName() + ": " + t.getMessage();
            }
        }
        if (a11) {
            try { Runtime.getRuntime().exec(new String[]{"am", "force-stop", "com.android.shell"}).waitFor(); } catch (Throwable ignored) { }
        }
        if (rec == null) fail("could not start recording: " + err);
        final AudioRecord r = rec;
        Runtime.getRuntime().addShutdownHook(new Thread(() -> { try { r.release(); } catch (Throwable ignored) { } }));

        int chOut = r.getChannelCount();
        JSONObject info = new JSONObject();
        info.put("rate", r.getSampleRate());
        info.put("channels", chOut);
        info.put("source", source);
        info.put("requested_device", deviceId);
        packet('I', info);

        // Watchdog: read() can block for good when another recorder takes the mic, so a dead PC link would never
        // be noticed by the audio loop. Probe the pipe with an empty keep-alive packet and watch the parent.
        Thread dog = new Thread(() -> {
            final int ppid = parentPid();
            while (true) {
                SystemClock.sleep(500);
                try {
                    packet('K', new byte[0], 0);
                } catch (Throwable t) {
                    try { r.release(); } catch (Throwable ignored) { }
                    System.exit(0);
                }
                if (ppid > 1 && parentPid() != ppid) { try { r.release(); } catch (Throwable ignored) { } System.exit(0); }
            }
        });
        dog.setDaemon(true);
        dog.start();

        byte[] data = new byte[r.getSampleRate() / 50 * chOut * 2];   // 20 ms
        long nextStatus = 0;
        while (true) {
            int n = r.read(data, 0, data.length);
            if (n < 0) fail("read error " + n);
            try {
                if (n > 0) packet('A', data, n);
                long now = SystemClock.uptimeMillis();
                if (now >= nextStatus) {
                    nextStatus = now + 1000;
                    packet('S', status(r));
                }
            } catch (IOException e) {           // PC closed the stream
                r.release();
                System.exit(0);
            }
        }
    }

    static int parentPid() {
        try {
            byte[] b = new byte[512];
            int n;
            try (java.io.FileInputStream in = new java.io.FileInputStream("/proc/self/stat")) { n = in.read(b); }
            String s = new String(b, 0, Math.max(n, 0));
            String[] f = s.substring(s.lastIndexOf(')') + 2).split(" ");
            return Integer.parseInt(f[1]);
        } catch (Throwable t) {
            return -1;
        }
    }

    static JSONObject status(AudioRecord r) throws Exception {
        JSONObject s = new JSONObject();
        AudioDeviceInfo routed = r.getRoutedDevice();
        if (routed != null) {
            s.put("routed_id", routed.getId());
            s.put("routed", typeName(routed.getType()) + (Build.VERSION.SDK_INT >= 28 && !routed.getAddress().isEmpty()
                    ? " (" + routed.getAddress() + ")" : ""));
        }
        if (Build.VERSION.SDK_INT >= 28) {
            JSONArray act = new JSONArray();
            try {
                List<MicrophoneInfo> l = r.getActiveMicrophones();
                for (MicrophoneInfo m : l) act.put(mic(m));
            } catch (Throwable ignored) { }
            s.put("active", act);
        }
        if (Build.VERSION.SDK_INT >= 29) {
            try {
                AudioRecordingConfiguration c = r.getActiveRecordingConfiguration();
                if (c != null) s.put("silenced", c.isClientSilenced());
            } catch (Throwable ignored) { }
        }
        return s;
    }
}
