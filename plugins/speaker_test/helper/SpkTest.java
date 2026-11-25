// Speaker test helper for the AndroUnderTheHood "Speaker test" plugin.
// Runs on the device with:  CLASSPATH=spktest.jar app_process / SpkTest list|play ...
//
//   list  -> JSON: output devices, stream volumes, audio mode
//   play <deviceId|-> <usage> <f0> <f1> <seconds> <channel> <level_db> <loops> [token]
//        usage:   media | call | alarm      channel: both | left | right
//        -> text lines on stdout:  I {json} | P <freq Hz> <seconds> | R {json routing} | D | E <message>
//
// Built against android.jar with javac --release 11 and d8 (see build_helper.sh).

import android.annotation.SuppressLint;
import android.app.Application;
import android.app.Instrumentation;
import android.content.AttributionSource;
import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.media.AudioAttributes;
import android.media.AudioDeviceInfo;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.os.Build;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.FileInputStream;
import java.io.PrintStream;
import java.lang.reflect.Constructor;
import java.lang.reflect.Field;
import java.lang.reflect.Method;

@SuppressLint({"PrivateApi", "DiscouragedPrivateApi", "SoonBlockedPrivateApi", "MissingPermission", "NewApi",
        "WrongConstant"})
public final class SpkTest {
    static final String PKG = "com.android.shell";
    static Context ctx;
    static PrintStream out;

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
        out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), true);
        try {
            if (Looper.getMainLooper() == null) Looper.prepareMainLooper();
        } catch (Throwable ignored) { }
        try {
            ctx = makeContext();
            if (args.length >= 1 && args[0].equals("list")) {
                out.println(list().toString());
                System.exit(0);
            } else if (args.length >= 3 && args[0].equals("setvol")) {
                int s = args[1].equals("call") ? AudioManager.STREAM_VOICE_CALL : args[1].equals("alarm")
                        ? AudioManager.STREAM_ALARM : AudioManager.STREAM_MUSIC;
                audio().setStreamVolume(s, Integer.parseInt(args[2]), 0);
                out.println(audio().getStreamVolume(s));
                System.exit(0);
            } else if (args.length >= 9 && args[0].equals("play")) {
                play(args[1].equals("-") ? 0 : Integer.parseInt(args[1]), args[2], Double.parseDouble(args[3]),
                        Double.parseDouble(args[4]), Double.parseDouble(args[5]), args[6], Double.parseDouble(args[7]),
                        Integer.parseInt(args[8]));
                System.exit(0);
            } else {
                System.err.println("usage: SpkTest list | play <deviceId|-> <usage> <f0> <f1> <seconds> <channel> <level_db> <loops>");
                System.exit(2);
            }
        } catch (Throwable t) {
            line("E " + t.getClass().getSimpleName() + ": " + t.getMessage());
            t.printStackTrace();
            System.exit(1);
        }
    }

    static void line(String s) {
        out.println(s);
        if (out.checkError()) {        // PC closed the stream
            restore();
            System.exit(0);
        }
    }

    // ------------------------------------------------------------------ names
    static String typeName(int t) {
        switch (t) {
            case 1: return "Earpiece";
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
            case 19: return "AUX line";
            case 20: return "IP";
            case 21: return "Bus";
            case 22: return "USB headset";
            case 23: return "Hearing aid";
            case 24: return "Built-in speaker (safe)";
            case 25: return "Remote submix";
            case 26: return "BLE headset";
            case 27: return "BLE speaker";
            case 29: return "HDMI eARC";
            case 30: return "BLE broadcast";
            case 31: return "Dock analog";
            case 18: return "Telephony";
            default: return "Type " + t;
        }
    }

    // ------------------------------------------------------------------ list
    static JSONObject list() throws Exception {
        JSONObject root = new JSONObject();
        root.put("sdk", Build.VERSION.SDK_INT);
        root.put("model", Build.MANUFACTURER + " " + Build.MODEL);
        root.put("uid", Process.myUid());
        AudioManager am = audio();
        JSONArray devs = new JSONArray();
        for (AudioDeviceInfo d : am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) {
            JSONObject o = new JSONObject();
            o.put("id", d.getId());
            o.put("type", d.getType());
            o.put("type_name", typeName(d.getType()));
            o.put("name", String.valueOf(d.getProductName()));
            if (Build.VERSION.SDK_INT >= 28) o.put("address", d.getAddress());
            JSONArray cc = new JSONArray();
            for (int c : d.getChannelCounts()) cc.put(c);
            o.put("channel_counts", cc);
            JSONArray cm = new JSONArray();
            for (int c : d.getChannelMasks()) cm.put(c);
            o.put("channel_masks", cm);
            JSONArray sr = new JSONArray();
            for (int r : d.getSampleRates()) sr.put(r);
            o.put("sample_rates", sr);
            devs.put(o);
        }
        root.put("devices", devs);
        JSONObject vol = new JSONObject();
        int[][] streams = {{AudioManager.STREAM_MUSIC, 0}, {AudioManager.STREAM_VOICE_CALL, 0}, {AudioManager.STREAM_ALARM, 0},
                {AudioManager.STREAM_RING, 0}};
        String[] names = {"media", "call", "alarm", "ring"};
        for (int i = 0; i < streams.length; i++) {
            int s = streams[i][0];
            JSONObject v = new JSONObject();
            v.put("cur", am.getStreamVolume(s));
            v.put("max", am.getStreamMaxVolume(s));
            try { v.put("muted", am.isStreamMute(s)); } catch (Throwable ignored) { }
            vol.put(names[i], v);
        }
        root.put("volumes", vol);
        root.put("mode", am.getMode());
        root.put("ringer", am.getRingerMode());
        root.put("music_active", am.isMusicActive());
        try { root.put("native_rate", am.getProperty(AudioManager.PROPERTY_OUTPUT_SAMPLE_RATE)); } catch (Throwable ignored) { }
        return root;
    }

    // ------------------------------------------------------------------ play
    static boolean modeChanged, commChanged;
    static int oldMode;

    static synchronized void restore() {
        try {
            AudioManager am = audio();
            if (commChanged && Build.VERSION.SDK_INT >= 31) am.clearCommunicationDevice();
            if (modeChanged) am.setMode(oldMode);
        } catch (Throwable ignored) { }
        commChanged = modeChanged = false;
    }

    static int parentPid() {
        try {
            byte[] b = new byte[512];
            int n;
            try (FileInputStream in = new FileInputStream("/proc/self/stat")) { n = in.read(b); }
            String s = new String(b, 0, Math.max(n, 0));
            return Integer.parseInt(s.substring(s.lastIndexOf(')') + 2).split(" ")[1]);
        } catch (Throwable t) {
            return -1;
        }
    }

    static void play(int deviceId, String usage, double f0, double f1, double secs, String channel, double levelDb,
                     int loops) throws Exception {
        AudioManager am = audio();
        AudioDeviceInfo dev = null;
        if (deviceId != 0) {
            for (AudioDeviceInfo d : am.getDevices(AudioManager.GET_DEVICES_OUTPUTS)) if (d.getId() == deviceId) dev = d;
            if (dev == null) { line("E output device " + deviceId + " is gone (unplugged?)"); return; }
        }
        // The earpiece is only used for calls: play as voice communication and route it explicitly.
        boolean earpiece = dev != null && dev.getType() == AudioDeviceInfo.TYPE_BUILTIN_EARPIECE;
        if (earpiece) usage = "call";
        int u = usage.equals("call") ? AudioAttributes.USAGE_VOICE_COMMUNICATION
                : usage.equals("alarm") ? AudioAttributes.USAGE_ALARM : AudioAttributes.USAGE_MEDIA;
        if (u == AudioAttributes.USAGE_VOICE_COMMUNICATION) {
            try {
                oldMode = am.getMode();
                am.setMode(AudioManager.MODE_IN_COMMUNICATION);
                modeChanged = true;
            } catch (Throwable t) { line("W could not switch to call mode: " + t.getMessage()); }
            if (Build.VERSION.SDK_INT >= 31 && dev != null) {
                try { commChanged = am.setCommunicationDevice(dev); } catch (Throwable t) { line("W " + t.getMessage()); }
            }
        }
        Runtime.getRuntime().addShutdownHook(new Thread(SpkTest::restore));

        final int rate = 48000;
        AudioAttributes attrs = new AudioAttributes.Builder().setUsage(u)
                .setContentType(u == AudioAttributes.USAGE_VOICE_COMMUNICATION ? AudioAttributes.CONTENT_TYPE_SPEECH
                        : AudioAttributes.CONTENT_TYPE_MUSIC).build();
        AudioFormat fmt = new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(rate)
                .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).build();
        int min = AudioTrack.getMinBufferSize(rate, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_16BIT);
        AudioTrack track = new AudioTrack.Builder().setAudioAttributes(attrs).setAudioFormat(fmt)
                .setBufferSizeInBytes(Math.max(min * 2, rate / 10 * 4)).setTransferMode(AudioTrack.MODE_STREAM).build();
        if (track.getState() != AudioTrack.STATE_INITIALIZED) { line("E AudioTrack did not initialise"); restore(); return; }
        if (dev != null) track.setPreferredDevice(dev);
        Runtime.getRuntime().addShutdownHook(new Thread(() -> { try { track.release(); } catch (Throwable ignored) { } }));

        // watchdog: exit when the PC side or our parent goes away
        Thread dog = new Thread(() -> {
            int ppid = parentPid();
            while (true) {
                SystemClock.sleep(400);
                if (ppid > 1 && parentPid() != ppid) { restore(); System.exit(0); }
            }
        });
        dog.setDaemon(true);
        dog.start();

        int stream = u == AudioAttributes.USAGE_VOICE_COMMUNICATION ? AudioManager.STREAM_VOICE_CALL
                : u == AudioAttributes.USAGE_ALARM ? AudioManager.STREAM_ALARM : AudioManager.STREAM_MUSIC;
        JSONObject info = new JSONObject();
        info.put("rate", rate);
        info.put("usage", usage);
        info.put("volume", am.getStreamVolume(stream));
        info.put("volume_max", am.getStreamMaxVolume(stream));
        line("I " + info);

        track.play();
        double amp = Math.pow(10, levelDb / 20.0) * 32767;
        boolean left = !channel.equals("right"), right = !channel.equals("left");
        int total = (int) (secs * rate);
        int fade = rate / 50;          // 20 ms fade in/out against clicks
        double k = f1 == f0 ? 0 : Math.log(f1 / f0);
        short[] buf = new short[rate / 50 * 2];
        long nextReport = 0;
        boolean routedSent = false;
        for (int loop = 0; loop < Math.max(1, loops); loop++) {
            double phase = 0;
            int n = 0;
            while (n < total) {
                int frames = Math.min(buf.length / 2, total - n);
                for (int i = 0; i < frames; i++, n++) {
                    double t = n / (double) rate;
                    double f = k == 0 ? f0 : f0 * Math.exp(k * t / secs);    // logarithmic (exponential) sweep
                    phase += 2 * Math.PI * f / rate;
                    if (phase > 2 * Math.PI) phase -= 2 * Math.PI;
                    double env = Math.min(1.0, Math.min(n, total - 1 - n) / (double) fade);
                    short s = (short) (Math.sin(phase) * amp * env);
                    buf[2 * i] = left ? s : 0;
                    buf[2 * i + 1] = right ? s : 0;
                }
                int w = track.write(buf, 0, frames * 2);
                if (w < 0) { line("E write error " + w); restore(); return; }
                long now = SystemClock.uptimeMillis();
                if (now >= nextReport) {
                    nextReport = now + 100;
                    // report what is audible now: the head position lags the write position by the buffer
                    int played = track.getPlaybackHeadPosition() % Math.max(1, total);
                    double t = played / (double) rate;
                    double f = k == 0 ? f0 : f0 * Math.exp(k * t / secs);
                    line(String.format(java.util.Locale.ROOT, "P %.1f %.2f %d", f, t, loop));
                    if (!routedSent && n > rate / 4) {
                        routedSent = true;
                        AudioDeviceInfo r = track.getRoutedDevice();
                        JSONObject ro = new JSONObject();
                        if (r != null) {
                            ro.put("id", r.getId());
                            ro.put("name", typeName(r.getType()) + (Build.VERSION.SDK_INT >= 28 && !r.getAddress().isEmpty()
                                    ? " (" + r.getAddress() + ")" : ""));
                        }
                        ro.put("volume", am.getStreamVolume(stream));
                        ro.put("volume_max", am.getStreamMaxVolume(stream));
                        line("R " + ro);
                    }
                }
            }
        }
        // let the buffer drain before stopping
        long end = SystemClock.uptimeMillis() + 1500;
        int target = total * Math.max(1, loops);
        while (track.getPlaybackHeadPosition() < target - rate / 100 && SystemClock.uptimeMillis() < end) {
            SystemClock.sleep(20);
        }
        track.stop();
        track.release();
        restore();
        line("D");
    }
}
