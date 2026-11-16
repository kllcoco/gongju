package cn.llmjourney.dict;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Calendar;
import java.util.Iterator;
import java.util.Locale;

/**
 * 闪卡的原生部分：读写 flash.json（和网页那边共用同一份数据）、FSRS 评分、通知栏闪卡、定时提醒。
 * FSRS 的公式和 app.js 里的 fsGrade 一模一样，两边算出来的下次时间相同。
 */
final class FlashCore {
    static final String CH = "flash";
    static final int NID = 1001;
    static final String A_ALARM = "cn.llmjourney.dict.FC_ALARM";
    static final String A_FLIP = "cn.llmjourney.dict.FC_FLIP";
    static final String A_GRADE = "cn.llmjourney.dict.FC_GRADE";
    static final String A_DISMISS = "cn.llmjourney.dict.FC_DISMISS";
    static final long MIN = 60000L, HOUR = 3600000L, DAY = 86400000L;
    static final double[] W = {0.4872, 1.4003, 3.7145, 13.8206, 5.1618, 1.2298, 0.8975, 0.031, 1.6474, 0.1367, 1.0461, 2.1072, 0.0793, 0.3246, 1.587, 0.2272, 2.8755};
    static final double F = 19.0 / 81.0, DECAY = -0.5;
    private static final Object LOCK = new Object();

    private FlashCore() {}

    /* ---------- 读写 ---------- */
    static String read(Context c) {
        synchronized (LOCK) {
            File f = new File(c.getFilesDir(), "flash.json");
            if (!f.exists()) return "";
            try (FileInputStream in = new FileInputStream(f)) {
                ByteArrayOutputStream bo = new ByteArrayOutputStream();
                byte[] b = new byte[65536];
                int n;
                while ((n = in.read(b)) > 0) bo.write(b, 0, n);
                return new String(bo.toByteArray(), StandardCharsets.UTF_8);
            } catch (IOException e) {
                return "";
            }
        }
    }

    static void write(Context c, String s) {
        synchronized (LOCK) {
            File f = new File(c.getFilesDir(), "flash.json");
            File tmp = new File(c.getFilesDir(), "flash.json.tmp");
            try (FileOutputStream o = new FileOutputStream(tmp)) {
                o.write(s.getBytes(StandardCharsets.UTF_8));
                o.getFD().sync();
            } catch (IOException e) {
                return;
            }
            if (!tmp.renameTo(f)) {
                f.delete();
                tmp.renameTo(f);
            }
        }
    }

    static JSONObject load(Context c) {
        try {
            String s = read(c);
            JSONObject o = s.isEmpty() ? new JSONObject() : new JSONObject(s);
            if (!o.has("c")) o.put("c", new JSONObject());
            if (!o.has("cfg")) o.put("cfg", new JSONObject());
            if (!o.has("nlog")) o.put("nlog", new JSONArray());
            return o;
        } catch (JSONException e) {
            return null;
        }
    }

    static void save(Context c, JSONObject o) {
        write(c, o.toString());
    }

    /* ---------- FSRS ---------- */
    static double clampD(double d) { return Math.min(10, Math.max(1, d)); }

    static double d0(int g) { return clampD(W[4] - (g - 3) * W[5]); }

    static double retr(double days, double s) { return Math.pow(1 + F * days / s, DECAY); }

    static double retrOf(JSONObject c, long now) {
        if (c.optInt("st", 0) == 0) return 0;
        double s = c.optDouble("s", 1);
        if (s <= 0) s = 0.1;
        return retr(Math.max(0, now - c.optLong("last", now)) / (double) DAY, s);
    }

    static void grade(JSONObject c, int g, long now) throws JSONException {
        int st = c.optInt("st", 0);
        double s = c.optDouble("s", 0), d = c.optDouble("d", 0);
        if (st == 0) {
            s = W[g - 1];
            d = d0(g);
        } else {
            double r = retr(Math.max(0, now - c.optLong("last", now)) / (double) DAY, Math.max(0.1, s));
            if (g == 1) {
                s = Math.min(s, W[11] * Math.pow(d, -W[12]) * (Math.pow(s + 1, W[13]) - 1) * Math.exp(W[14] * (1 - r)));
            } else {
                s = s * (Math.exp(W[8]) * (11 - d) * Math.pow(s, -W[9]) * (Math.exp(W[10] * (1 - r)) - 1)
                        * (g == 2 ? W[15] : 1) * (g == 4 ? W[16] : 1) + 1);
            }
            d = clampD(W[7] * d0(4) + (1 - W[7]) * (d - W[6] * (g - 3)));
        }
        s = Math.max(0.1, s);
        c.put("s", s);
        c.put("d", d);
        c.put("reps", c.optInt("reps", 0) + 1);
        c.put("last", now);
        c.put("t", now);
        if (g == 1) {
            c.put("lapses", c.optInt("lapses", 0) + 1);
            c.put("st", 2);
            c.put("due", now + 10 * MIN);
        } else {
            c.put("st", 1);
            long days = Math.min(365, Math.max(1, Math.round(s)));
            c.put("due", now + days * DAY);
        }
    }

    static String level(JSONObject c) {
        int st = c.optInt("st", 0);
        double s = c.optDouble("s", 0);
        if (st == 0) return "新学";
        if (st == 2 || s < 3) return "不牢";
        return s < 21 ? "巩固中" : "已掌握";
    }

    static String when(long ms) {
        long m = Math.round(ms / (double) MIN);
        if (m < 60) return Math.max(1, m) + "分钟";
        if (m < 1440) return Math.round(m / 60.0) + "小时";
        return Math.round(m / 1440.0) + "天";
    }

    /* ---------- 选卡 ---------- */
    /** 到期的卡里，回想概率最低（最快要忘）的那张；只看有纯文本的卡。 */
    static String pickDue(JSONObject o, long now) {
        JSONObject cs = o.optJSONObject("c");
        if (cs == null) return null;
        String best = null;
        double bestR = 2;
        long bestDue = Long.MAX_VALUE;
        Iterator<String> it = cs.keys();
        while (it.hasNext()) {
            String id = it.next();
            JSONObject c = cs.optJSONObject(id);
            if (c == null || c.optString("ft", "").isEmpty()) continue;
            long due = c.optLong("due", Long.MAX_VALUE);
            if (due > now) continue;
            double r = retrOf(c, now);
            if (r < bestR || (r == bestR && due < bestDue)) {
                best = id;
                bestR = r;
                bestDue = due;
            }
        }
        return best;
    }

    static long earliestDue(JSONObject o) {
        JSONObject cs = o.optJSONObject("c");
        long t = Long.MAX_VALUE;
        if (cs == null) return t;
        Iterator<String> it = cs.keys();
        while (it.hasNext()) {
            JSONObject c = cs.optJSONObject(it.next());
            if (c == null || c.optString("ft", "").isEmpty()) continue;
            t = Math.min(t, c.optLong("due", Long.MAX_VALUE));
        }
        return t;
    }

    /* ---------- 设置、免打扰、每日上限 ---------- */
    static boolean enabled(Context ctx, JSONObject o) {
        JSONObject cfg = o.optJSONObject("cfg");
        if (cfg == null || !cfg.optBoolean("on", false)) return false;
        return permitted(ctx);
    }

    static boolean permitted(Context ctx) {
        return Build.VERSION.SDK_INT < 33
                || ctx.checkSelfPermission("android.permission.POST_NOTIFICATIONS") == PackageManager.PERMISSION_GRANTED;
    }

    static boolean quietAt(JSONObject o, long t) {
        JSONObject cfg = o.optJSONObject("cfg");
        int q0 = cfg == null ? 22 : cfg.optInt("q0", 22), q1 = cfg == null ? 8 : cfg.optInt("q1", 8);
        if (q0 == q1) return false;
        Calendar cal = Calendar.getInstance();
        cal.setTimeInMillis(t);
        int h = cal.get(Calendar.HOUR_OF_DAY);
        return q0 > q1 ? (h >= q0 || h < q1) : (h >= q0 && h < q1);
    }

    /** t 落在免打扰时段里时，推到这一段结束（早上 q1 点）。 */
    static long afterQuiet(JSONObject o, long t) {
        if (!quietAt(o, t)) return t;
        JSONObject cfg = o.optJSONObject("cfg");
        int q1 = cfg == null ? 8 : cfg.optInt("q1", 8);
        Calendar cal = Calendar.getInstance();
        cal.setTimeInMillis(t);
        int h = cal.get(Calendar.HOUR_OF_DAY);
        if (h >= q1) cal.add(Calendar.DAY_OF_MONTH, 1);
        cal.set(Calendar.HOUR_OF_DAY, q1);
        cal.set(Calendar.MINUTE, 0);
        cal.set(Calendar.SECOND, 0);
        cal.set(Calendar.MILLISECOND, 0);
        return cal.getTimeInMillis();
    }

    static String today(long now) {
        Calendar c = Calendar.getInstance();
        c.setTimeInMillis(now);
        return String.format(Locale.US, "%04d-%02d-%02d", c.get(Calendar.YEAR), c.get(Calendar.MONTH) + 1, c.get(Calendar.DAY_OF_MONTH));
    }

    static void rollDay(JSONObject o, long now) throws JSONException {
        String d = today(now);
        if (!d.equals(o.optString("day", ""))) {
            o.put("day", d);
            o.put("shown", 0);
        }
    }

    static boolean capped(JSONObject o) {
        JSONObject cfg = o.optJSONObject("cfg");
        int cap = cfg == null ? 20 : cfg.optInt("cap", 20);
        return o.optInt("shown", 0) >= cap;
    }

    /* ---------- 通知栏闪卡 ---------- */
    static void ensureChannel(Context ctx) {
        NotificationManager nm = ctx.getSystemService(NotificationManager.class);
        if (nm.getNotificationChannel(CH) == null) {
            NotificationChannel ch = new NotificationChannel(CH, "闪卡复习", NotificationManager.IMPORTANCE_DEFAULT);
            ch.setDescription("到复习时间时，在通知栏弹出一张闪卡");
            nm.createNotificationChannel(ch);
        }
    }

    private static PendingIntent action(Context ctx, String act, String id, int g) {
        Intent i = new Intent(ctx, FlashReceiver.class).setAction(act).putExtra("id", id).putExtra("g", g);
        int code = (act + "|" + id + "|" + g).hashCode();
        return PendingIntent.getBroadcast(ctx, code, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    static void post(Context ctx, JSONObject o, String id, boolean back) {
        JSONObject card = o.optJSONObject("c") == null ? null : o.optJSONObject("c").optJSONObject(id);
        if (card == null) return;
        ensureChannel(ctx);
        long now = System.currentTimeMillis();
        String src = card.optString("src", "闪卡") + " · " + level(card);
        String front = card.optString("ft", ""), backTxt = card.optString("bt", "");

        RemoteViews small = new RemoteViews(ctx.getPackageName(), R.layout.fc_small);
        small.setTextViewText(R.id.fc_src, src + (back ? "" : " · 下拉展开翻面"));
        small.setTextViewText(R.id.fc_front, front);

        RemoteViews big = new RemoteViews(ctx.getPackageName(), R.layout.fc_big);
        big.setTextViewText(R.id.fc_src, src);
        big.setTextViewText(R.id.fc_front, front);
        big.setTextViewText(R.id.fc_back, backTxt);
        big.setViewVisibility(R.id.fc_div, back ? View.VISIBLE : View.GONE);
        big.setViewVisibility(R.id.fc_back, back ? View.VISIBLE : View.GONE);
        big.setViewVisibility(R.id.fc_flip, back ? View.GONE : View.VISIBLE);
        big.setViewVisibility(R.id.fc_grades, back ? View.VISIBLE : View.GONE);
        big.setOnClickPendingIntent(R.id.fc_flip, action(ctx, A_FLIP, id, 0));
        int[] ids = {R.id.fc_g1, R.id.fc_g2, R.id.fc_g3, R.id.fc_g4};
        String[] names = {"忘了", "有点难", "记得", "很简单"};
        for (int g = 1; g <= 4; g++) {
            String sub;
            if (g == 1) {
                sub = "10分钟";
            } else {
                try {
                    JSONObject tmp = new JSONObject(card.toString());
                    grade(tmp, g, now);
                    sub = when(tmp.optLong("due", now) - now);
                } catch (JSONException e) {
                    sub = "";
                }
            }
            big.setTextViewText(ids[g - 1], names[g - 1] + "\n" + sub);
            big.setOnClickPendingIntent(ids[g - 1], action(ctx, A_GRADE, id, g));
        }

        Intent open = new Intent(ctx, MainActivity.class).putExtra("go", "fc")
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPi = PendingIntent.getActivity(ctx, 9, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);

        Notification n = new Notification.Builder(ctx, CH)
                .setSmallIcon(R.drawable.ic_flash)
                .setStyle(new Notification.DecoratedCustomViewStyle())
                .setCustomContentView(small)
                .setCustomBigContentView(big)
                .setContentTitle(src)
                .setContentText(front)
                .setContentIntent(openPi)
                .setDeleteIntent(action(ctx, A_DISMISS, id, 0))
                .setOnlyAlertOnce(true)
                .setAutoCancel(false)
                .setShowWhen(false)
                .setCategory(Notification.CATEGORY_REMINDER)
                .build();
        ctx.getSystemService(NotificationManager.class).notify(NID, n);
    }

    static void cancel(Context ctx) {
        ctx.getSystemService(NotificationManager.class).cancel(NID);
    }

    /**
     * 到点了，或者刚评完一张卡：如果有到期的卡、不在免打扰时段、今天还没到上限，就弹下一张。
     * afterGrade 为 true 时是用户刚在通知里评完分，接着出下一张，不受“1 小时内别重复弹”的限制。
     */
    static void next(Context ctx, JSONObject o, long now, boolean afterGrade) throws JSONException {
        if (!enabled(ctx, o)) {
            cancel(ctx);
            schedule(ctx, o, now);
            return;
        }
        rollDay(o, now);
        String id = pickDue(o, now);
        boolean ok = id != null && !quietAt(o, now) && !capped(o) && (afterGrade || now >= o.optLong("snooze", 0));
        if (ok) {
            post(ctx, o, id, false);
            o.put("shown", o.optInt("shown", 0) + 1);
            o.put("snooze", now + HOUR);   // 用户没理它的话，一小时后再提醒
        } else if (afterGrade) {
            cancel(ctx);
            o.put("snooze", 0);
        }
        save(ctx, o);
        schedule(ctx, o, now);
    }

    static void schedule(Context ctx, JSONObject o, long now) {
        AlarmManager am = ctx.getSystemService(AlarmManager.class);
        Intent i = new Intent(ctx, FlashReceiver.class).setAction(A_ALARM);
        PendingIntent pi = PendingIntent.getBroadcast(ctx, 77, i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        if (!enabled(ctx, o)) {
            am.cancel(pi);
            return;
        }
        long t = earliestDue(o);
        if (t == Long.MAX_VALUE) {
            am.cancel(pi);
            return;
        }
        t = Math.max(t, now + MIN);
        t = Math.max(t, o.optLong("snooze", 0));
        if (capped(o) && today(t).equals(o.optString("day", ""))) {
            Calendar cal = Calendar.getInstance();
            cal.setTimeInMillis(now);
            cal.add(Calendar.DAY_OF_MONTH, 1);
            cal.set(Calendar.HOUR_OF_DAY, 0);
            cal.set(Calendar.MINUTE, 1);
            t = Math.max(t, cal.getTimeInMillis());
        }
        t = afterQuiet(o, t);
        am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, t, pi);
    }

    static void reschedule(Context ctx) {
        JSONObject o = load(ctx);
        if (o != null) schedule(ctx, o, System.currentTimeMillis());
    }

    /** “现在弹一张试试”：不管免打扰和上限，有到期的弹到期的，没有就弹最快到期的那张。 */
    static String test(Context ctx) {
        JSONObject o = load(ctx);
        if (o == null) return "none";
        if (!enabled(ctx, o)) return "perm";
        long now = System.currentTimeMillis();
        String id = pickDue(o, now);
        if (id == null) {
            JSONObject cs = o.optJSONObject("c");
            long best = Long.MAX_VALUE;
            if (cs != null) {
                Iterator<String> it = cs.keys();
                while (it.hasNext()) {
                    String k = it.next();
                    JSONObject c = cs.optJSONObject(k);
                    if (c == null || c.optString("ft", "").isEmpty()) continue;
                    if (c.optLong("due", Long.MAX_VALUE) < best) {
                        best = c.optLong("due", Long.MAX_VALUE);
                        id = k;
                    }
                }
            }
        }
        if (id == null) return "none";
        post(ctx, o, id, false);
        return "ok";
    }
}
