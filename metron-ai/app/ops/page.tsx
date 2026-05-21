"use client";

import { useState, useEffect } from "react";
import { signIn, signOut, fetchAuthSession, confirmSignIn } from "aws-amplify/auth";

export default function OpsLoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [step, setStep] = useState<"login" | "new-password">("login");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // Always force sign-out on mount so visiting /ops never auto-logs in
  useEffect(() => {
    signOut().catch(() => {});
    document.cookie = "metron_session=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
    document.cookie = "metron_role=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
  }, []);

  const finalize = async () => {
    let token = "";
    for (let i = 0; i < 20; i++) {
      try {
        const session = await fetchAuthSession();
        if (session.tokens?.idToken) { token = session.tokens.idToken.toString(); break; }
      } catch (_) {}
      await new Promise(r => setTimeout(r, 100));
    }
    const qr = await fetch("/api/quota", { headers: { Authorization: `Bearer ${token}` } });
    if (!qr.ok) {
      const d = await qr.json().catch(() => ({}));
      await signOut();
      setError(d.detail || "Access denied. This portal is restricted.");
      setLoading(false);
      return;
    }
    const qd = await qr.json();
    if (qd.role !== "super_admin") {
      await signOut();
      setError("Access denied. This portal is for platform administrators only.");
      setLoading(false);
      return;
    }
    document.cookie = "metron_session=1; path=/; SameSite=Lax; max-age=28800";
    document.cookie = "metron_role=super_admin; path=/; SameSite=Lax; max-age=28800";
    sessionStorage.setItem("metron_user_email", qd.email || email || "");
    window.location.href = "/super";
  };

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true); setError("");
    try {
      try { await signOut(); } catch (_) {}
      const output = await signIn({ username: email.trim(), password });
      if (output.nextStep?.signInStep === "CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED") {
        setStep("new-password"); setLoading(false); return;
      }
      if (output.isSignedIn) await finalize();
      else { setError("Sign-in incomplete. Please contact your administrator."); setLoading(false); }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Invalid credentials");
      setLoading(false);
    }
  };

  const handleNewPassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true); setError("");
    try {
      const output = await confirmSignIn({ challengeResponse: newPassword, options: { userAttributes: { name: email || "Admin" } } });
      if (output.isSignedIn) await finalize();
      else { setError("Could not set new password. Please try again."); setLoading(false); }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to set password");
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen w-screen bg-[var(--color-background)]">
      {/* Left Brand Panel */}
      <div className="hidden lg:flex lg:w-1/2 flex-col justify-between p-16 relative overflow-hidden bg-gradient-to-br from-[#001e2c] via-[#004c69] to-[#00668a]">
        <div className="absolute -top-32 -left-32 w-[500px] h-[500px] rounded-full bg-white/5 blur-3xl" />
        <div className="absolute bottom-0 right-0 w-[600px] h-[600px] rounded-full bg-[var(--color-primary-container)] opacity-10 blur-3xl" />
        <div className="relative z-10 flex items-center gap-3">
          <div className="w-12 h-12 rounded-xl bg-white/10 backdrop-blur-md flex items-center justify-center border border-white/20">
            <span className="material-symbols-outlined text-white text-2xl">shield_person</span>
          </div>
          <span className="text-white font-headline text-2xl font-black tracking-tighter">MetronAI</span>
        </div>
        <div className="relative z-10 space-y-8 max-w-lg">
          <div className="inline-flex items-center gap-3 px-4 py-2 rounded-full bg-white/10 backdrop-blur-sm border border-white/20">
            <span className="w-2.5 h-2.5 rounded-full bg-[#6bff8f] pulse-orb" />
            <span className="text-white/90 text-[10px] font-black uppercase tracking-[0.2em]">Platform Operations Console</span>
          </div>
          <div className="space-y-4">
            <h1 className="font-headline text-5xl xl:text-6xl font-extrabold text-white leading-[1.1] tracking-tight">
              Platform<br />Operations<br />
              <span className="text-[var(--color-primary-container)]">Console.</span>
            </h1>
            <p className="text-white/60 text-lg leading-relaxed font-medium">
              Restricted access. Manage tenants, quotas, and platform-wide settings.
            </p>
          </div>
        </div>
        <div className="relative z-10 flex items-center gap-8 pt-8 border-t border-white/5">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-white/40 text-lg">lock</span>
            <span className="text-white/40 text-[10px] font-bold uppercase tracking-widest">Restricted Access</span>
          </div>
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-white/40 text-lg">visibility_off</span>
            <span className="text-white/40 text-[10px] font-bold uppercase tracking-widest">All Access Logged</span>
          </div>
        </div>
      </div>

      {/* Right Login Area */}
      <div className="flex-1 flex flex-col items-center justify-center p-8 lg:p-16 bg-[var(--color-background)] animate-fade-in">
        <div className="w-full max-w-[420px] space-y-10">
          <div className="lg:hidden flex items-center gap-3 mb-10">
            <div className="w-11 h-11 rounded-xl bg-[var(--color-primary)] flex items-center justify-center shadow-lg shadow-primary/20">
              <span className="material-symbols-outlined text-white">shield_person</span>
            </div>
            <span className="font-headline text-2xl font-black text-[var(--color-primary)] tracking-tighter">MetronAI Ops</span>
          </div>

          {step === "login" ? (
            <>
              <div className="space-y-2">
                <div className="flex items-center gap-2">
                  <span className="w-1.5 h-1.5 rounded-full bg-error" />
                  <span className="text-[10px] font-black uppercase tracking-[0.2em] text-error">Restricted</span>
                </div>
                <h2 className="font-headline text-3xl font-extrabold text-[var(--color-on-surface)] tracking-tight">Platform Console</h2>
                <p className="text-[var(--color-on-surface-variant)] font-medium">Sign in with your administrator credentials.</p>
              </div>

              <form className="space-y-6" autoComplete="on" onSubmit={handleLogin}>
                <div className="space-y-4">
                  <div className="space-y-1.5">
                    <label className="text-[10px] font-black uppercase tracking-[0.1em] text-[var(--color-on-surface-variant)]">Admin Email</label>
                    <div className="relative group">
                      <span className="material-symbols-outlined absolute left-4 top-1/2 -translate-y-1/2 text-[var(--color-outline)] group-focus-within:text-[var(--color-primary)] transition-colors">mail</span>
                      <input type="email" placeholder="admin@platform.com" value={email}
                        onChange={e => setEmail(e.target.value)} required
                        className="w-full pl-12 pr-4 py-4 rounded-xl bg-[var(--color-surface-container-low)] border-none ring-1 ring-[#00668a]/10 focus:ring-2 focus:ring-[var(--color-primary)] transition-all outline-none" />
                    </div>
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-[10px] font-black uppercase tracking-[0.1em] text-[var(--color-on-surface-variant)]">Password</label>
                    <div className="relative group">
                      <span className="material-symbols-outlined absolute left-4 top-1/2 -translate-y-1/2 text-[var(--color-outline)] group-focus-within:text-[var(--color-primary)] transition-colors">lock</span>
                      <input type={showPassword ? "text" : "password"} placeholder="••••••••" value={password}
                        onChange={e => setPassword(e.target.value)} required
                        className="w-full pl-12 pr-12 py-4 rounded-xl bg-[var(--color-surface-container-low)] border-none ring-1 ring-[#00668a]/10 focus:ring-2 focus:ring-[var(--color-primary)] transition-all outline-none" />
                      <button type="button" onClick={() => setShowPassword(!showPassword)}
                        className="absolute right-4 top-1/2 -translate-y-1/2 text-[var(--color-outline)] hover:text-[var(--color-primary)] transition-colors">
                        <span className="material-symbols-outlined text-xl">{showPassword ? "visibility_off" : "visibility"}</span>
                      </button>
                    </div>
                  </div>
                </div>

                {error && <p className="text-sm font-semibold text-red-500 bg-red-50 px-4 py-3 rounded-xl">{error}</p>}

                <button type="submit" disabled={loading}
                  className="w-full py-4 rounded-xl btn-primary shadow-xl shadow-[#00668a]/10 flex items-center justify-center gap-3 disabled:opacity-50">
                  {loading
                    ? <span className="material-symbols-outlined animate-spin">progress_activity</span>
                    : <>Authenticate <span className="material-symbols-outlined">arrow_forward</span></>
                  }
                </button>
              </form>
            </>
          ) : (
            <>
              <div className="space-y-2">
                <h2 className="font-headline text-3xl font-extrabold text-[var(--color-on-surface)] tracking-tight">Set your password</h2>
                <p className="text-[var(--color-on-surface-variant)] font-medium">First login detected. Set a permanent password to continue.</p>
              </div>

              <form className="space-y-6" onSubmit={handleNewPassword}>
                <div className="space-y-1.5">
                  <label className="text-[10px] font-black uppercase tracking-[0.1em] text-[var(--color-on-surface-variant)]">New Password</label>
                  <div className="relative group">
                    <span className="material-symbols-outlined absolute left-4 top-1/2 -translate-y-1/2 text-[var(--color-outline)] group-focus-within:text-[var(--color-primary)] transition-colors">lock</span>
                    <input type="password" placeholder="Choose a strong password" value={newPassword}
                      onChange={e => setNewPassword(e.target.value)} required minLength={8}
                      className="w-full pl-12 pr-4 py-4 rounded-xl bg-[var(--color-surface-container-low)] border-none ring-1 ring-[#00668a]/10 focus:ring-2 focus:ring-[var(--color-primary)] transition-all outline-none" />
                  </div>
                  <p className="text-[10px] text-[var(--color-on-surface-variant)] opacity-60 pl-1">Min. 8 characters</p>
                </div>

                {error && <p className="text-sm font-semibold text-red-500 bg-red-50 px-4 py-3 rounded-xl">{error}</p>}

                <button type="submit" disabled={loading}
                  className="w-full py-4 rounded-xl btn-primary shadow-xl shadow-[#00668a]/10 flex items-center justify-center gap-3 disabled:opacity-50">
                  {loading
                    ? <span className="material-symbols-outlined animate-spin">progress_activity</span>
                    : <>Set Password & Enter <span className="material-symbols-outlined">arrow_forward</span></>
                  }
                </button>
              </form>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
