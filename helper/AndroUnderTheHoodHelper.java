import android.content.Context;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.drawable.Drawable;
import android.os.Looper;
import android.util.Base64;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.util.List;

/**
 * AndroUnderTheHood on-device helper, run with:
 *   CLASSPATH=/data/local/tmp/androunderthehood-helper.jar app_process / AndroUnderTheHoodHelper <command> [args]
 *
 * Commands:
 *   labels              one line per installed package: "L\t<package>\t<label>"
 *   icons <size> pkg... one line per package: "I\t<package>\t<base64 PNG>"
 *
 * Uses the system context (ActivityThread.systemMain) so it needs root or shell.
 */
public final class AndroUnderTheHoodHelper {

    private AndroUnderTheHoodHelper() {
    }

    private static Context systemContext() throws Exception {
        if (Looper.getMainLooper() == null) {
            Looper.prepareMainLooper();
        }
        Class<?> at = Class.forName("android.app.ActivityThread");
        Object thread = at.getMethod("systemMain").invoke(null);
        return (Context) at.getMethod("getSystemContext").invoke(thread);
    }

    private static String clean(CharSequence s) {
        return s == null ? "" : s.toString().replace('\t', ' ').replace('\n', ' ').replace('\r', ' ').trim();
    }

    public static void main(String... args) throws Exception {
        if (args.length < 1) {
            System.err.println("usage: AndroUnderTheHoodHelper labels | icons <size> <pkg>...");
            System.exit(2);
        }
        PrintStream out = new PrintStream(new java.io.FileOutputStream(java.io.FileDescriptor.out), false, "UTF-8");
        Context ctx = systemContext();
        PackageManager pm = ctx.getPackageManager();
        int flags = PackageManager.MATCH_UNINSTALLED_PACKAGES | PackageManager.MATCH_DISABLED_COMPONENTS;

        if ("labels".equals(args[0])) {
            List<ApplicationInfo> apps = pm.getInstalledApplications(flags);
            for (ApplicationInfo ai : apps) {
                String label;
                try {
                    label = clean(ai.loadLabel(pm));
                } catch (Throwable t) {
                    label = "";
                }
                out.print("L\t" + ai.packageName + "\t" + label + "\n");
            }
        } else if ("icons".equals(args[0]) && args.length >= 2) {
            int size = Integer.parseInt(args[1]);
            for (int i = 2; i < args.length; i++) {
                String pkg = args[i];
                try {
                    ApplicationInfo ai = pm.getApplicationInfo(pkg, flags);
                    Drawable d = ai.loadIcon(pm);
                    Bitmap bmp = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
                    Canvas c = new Canvas(bmp);
                    d.setBounds(0, 0, size, size);
                    d.draw(c);
                    ByteArrayOutputStream bos = new ByteArrayOutputStream();
                    bmp.compress(Bitmap.CompressFormat.PNG, 100, bos);
                    out.print("I\t" + pkg + "\t" + Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP) + "\n");
                } catch (Throwable t) {
                    out.print("E\t" + pkg + "\t" + clean(t.toString()) + "\n");
                }
            }
        } else if ("activities".equals(args[0])) {
            // one line per activity:
            // A \t pkg \t class \t exported \t enabled \t launcher \t ownLabel \t label \t permission \t launchMode \t taskAffinity
            java.util.Set<String> launchers = new java.util.HashSet<>();
            android.content.Intent li = new android.content.Intent(android.content.Intent.ACTION_MAIN);
            li.addCategory(android.content.Intent.CATEGORY_LAUNCHER);
            for (android.content.pm.ResolveInfo ri : pm.queryIntentActivities(li,
                    PackageManager.MATCH_DISABLED_COMPONENTS | PackageManager.MATCH_ALL)) {
                launchers.add(ri.activityInfo.packageName + "/" + ri.activityInfo.name);
            }
            java.util.List<String> pkgs = new java.util.ArrayList<>();
            if (args.length > 1) {
                for (int i = 1; i < args.length; i++) {
                    pkgs.add(args[i]);
                }
            } else {
                for (ApplicationInfo ai : pm.getInstalledApplications(flags)) {
                    pkgs.add(ai.packageName);
                }
            }
            int aflags = PackageManager.GET_ACTIVITIES | PackageManager.MATCH_DISABLED_COMPONENTS
                    | PackageManager.MATCH_DISABLED_UNTIL_USED_COMPONENTS | PackageManager.MATCH_UNINSTALLED_PACKAGES;
            for (String pkg : pkgs) {
                android.content.pm.PackageInfo pi;
                try {
                    pi = pm.getPackageInfo(pkg, aflags);   // per package: avoids binder size limits
                } catch (Throwable t) {
                    out.print("E\t" + pkg + "\t" + clean(t.toString()) + "\n");
                    continue;
                }
                if (pi.activities == null) {
                    continue;
                }
                for (android.content.pm.ActivityInfo a : pi.activities) {
                    boolean enabled;
                    try {
                        int st = pm.getComponentEnabledSetting(new android.content.ComponentName(pkg, a.name));
                        enabled = st == PackageManager.COMPONENT_ENABLED_STATE_ENABLED
                                || (st == PackageManager.COMPONENT_ENABLED_STATE_DEFAULT && a.enabled);
                    } catch (Throwable t) {
                        enabled = a.enabled;
                    }
                    boolean own = a.labelRes != 0 || a.nonLocalizedLabel != null;
                    String label;
                    try {
                        label = clean(a.loadLabel(pm));
                    } catch (Throwable t) {
                        label = "";
                    }
                    out.print("A\t" + pkg + "\t" + a.name + "\t" + (a.exported ? 1 : 0) + "\t" + (enabled ? 1 : 0)
                            + "\t" + (launchers.contains(pkg + "/" + a.name) ? 1 : 0) + "\t" + (own ? 1 : 0)
                            + "\t" + label + "\t" + (a.permission == null ? "" : a.permission)
                            + "\t" + a.launchMode + "\t" + (a.taskAffinity == null ? "" : a.taskAffinity) + "\n");
                }
            }
        } else if ("procs".equals(args[0])) {
            // Magisk-style process list for the denylist UI:
            //   A \t pkg \t system(0/1) \t label        once per package
            //   P \t pkg \t process \t isolated(0/1)    once per distinct process
            for (ApplicationInfo ai : pm.getInstalledApplications(flags)) {
                String pkg = ai.packageName;
                String label;
                try {
                    label = clean(ai.loadLabel(pm));
                } catch (Throwable t) {
                    label = "";
                }
                boolean sys = (ai.flags & ApplicationInfo.FLAG_SYSTEM) != 0;
                out.print("A\t" + pkg + "\t" + (sys ? 1 : 0) + "\t" + label + "\n");
                java.util.LinkedHashSet<String> seen = new java.util.LinkedHashSet<>();
                seen.add((ai.processName == null ? pkg : ai.processName) + "\t0");
                int base = PackageManager.MATCH_DISABLED_COMPONENTS | PackageManager.MATCH_DISABLED_UNTIL_USED_COMPONENTS;
                int[] kinds = {PackageManager.GET_ACTIVITIES, PackageManager.GET_RECEIVERS, PackageManager.GET_PROVIDERS,
                        PackageManager.GET_SERVICES};
                for (int kind : kinds) {   // one call per component type: big apps overflow a single binder reply
                    java.util.List<android.content.pm.ComponentInfo> comps = new java.util.ArrayList<>();
                    try {
                        android.content.pm.PackageInfo pi = pm.getPackageInfo(pkg, base | kind);
                        if (pi.activities != null) java.util.Collections.addAll(comps, pi.activities);
                        if (pi.receivers != null) java.util.Collections.addAll(comps, pi.receivers);
                        if (pi.providers != null) java.util.Collections.addAll(comps, pi.providers);
                        if (pi.services != null) java.util.Collections.addAll(comps, pi.services);
                    } catch (Throwable t) {
                        continue;
                    }
                    for (android.content.pm.ComponentInfo c : comps) {
                        String proc = c.processName == null ? pkg : c.processName;
                        boolean iso = c instanceof android.content.pm.ServiceInfo
                                && (((android.content.pm.ServiceInfo) c).flags
                                & android.content.pm.ServiceInfo.FLAG_ISOLATED_PROCESS) != 0;
                        seen.add(iso ? proc + ":" + c.name + "	1" : proc + "	0");
                    }
                }
                for (String s : seen) {
                    out.print("P\t" + pkg + "\t" + s + "\n");
                }
            }
        } else {
            System.err.println("unknown command");
            System.exit(2);
        }
        out.flush();
        System.exit(0);
    }
}
