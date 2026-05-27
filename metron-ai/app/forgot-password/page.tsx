"use client";

import { useState } from "react";
import { resetPassword, confirmResetPassword } from "aws-amplify/auth";

export default function ForgotPasswordPage() {
  const [step, setStep] = useState<"request" | "confirm">("request");
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleRequest = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await resetPassword({ username: email.trim() });
      setStep("confirm");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Failed to send reset code. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  const handleConfirm = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (newPassword.length < 8) { setError("Password must be at least 8 characters."); return; }
    if (newPassword !== confirmPassword) { setError("Passwords do not match."); return; }
    setLoading(true);
    try {
      await confirmResetPassword({
        username: email.trim(),
        confirmationCode: code.trim(),
        newPassword,
      });
      window.location.href = "/?passwordReset=1";
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Invalid code or password. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex">
      {/* ─── Left Brand Panel ─────────────────────────────────────── */}
      <div className="hidden lg:flex lg:w-1/2 flex-col justify-between p-12 relative overflow-hidden bg-gradient-to-br from-[#001e2c] via-[#004c69] to-[#00668a]">
        <div className="absolute -top-32 -left-32 w-96 h-96 rounded-full bg-white/5 blur-3xl" />
        <div className="absolute bottom-0 right-0 w-[500px] h-[500px] rounded-full bg-[#38bdf8]/10 blur-3xl" />

        <div className="relative z-10 flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-white/10 flex items-center justify-center">
            <span className="material-symbols-outlined text-white text-xl">monitor_heart</span>
          </div>
          <span className="text-white font-headline text-2xl font-black tracking-tighter">MetronAI</span>
        </div>

        <div className="relative z-10 space-y-6">
          <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-white/10 border border-white/20">
            <span className="w-2 h-2 rounded-full bg-[#6bff8f] pulse-orb" />
            <span className="text-white/80 text-xs font-bold uppercase tracking-widest">
              {step === "request" ? "Reset your password" : "Check your inbox"}
            </span>
          </div>
          <h1 className="font-headline text-4xl xl:text-5xl font-extrabold text-white leading-tight tracking-tight">
            {step === "request" ? (
              <>Forgot your<br />password?<br /><span className="text-[#38bdf8]">No problem.</span></>
            ) : (
              <>Enter the<br />code we<br /><span className="text-[#38bdf8]">sent you.</span></>
            )}
          </h1>
          <p className="text-white/60 text-lg leading-relaxed max-w-sm">
            {step === "request"
              ? "Enter your work email and we'll send you a reset code instantly."
              : `We sent a 6-digit code to ${email}. Enter it below with your new password.`}
          </p>
        </div>

        <div className="relative z-10 flex items-center gap-6">
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-white/40 text-sm">verified_user</span>
            <span className="text-white/40 text-xs font-medium uppercase tracking-wider">SOC 2 Compliant</span>
          </div>
          <div className="w-px h-4 bg-white/20" />
          <div className="flex items-center gap-2">
            <span className="material-symbols-outlined text-white/40 text-sm">lock</span>
            <span className="text-white/40 text-xs font-medium uppercase tracking-wider">Enterprise SSO</span>
          </div>
        </div>
      </div>

      {/* ─── Right Form ───────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col items-center justify-center px-6 py-12 bg-[var(--color-background)] animate-fade-in">
        <div className="lg:hidden flex items-center gap-2 mb-10">
          <div className="w-9 h-9 rounded-xl bg-[var(--color-primary)] flex items-center justify-center">
            <span className="material-symbols-outlined text-white text-lg">monitor_heart</span>
          </div>
          <span className="font-headline text-xl font-black text-[var(--color-primary)] tracking-tighter">MetronAI</span>
        </div>

        <div className="w-full max-w-md">
          {/* Step indicator */}
          <div className="flex items-center gap-3 mb-8">
            <div className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold ${step === "request" ? "bg-[var(--color-primary)] text-white" : "bg-[var(--color-secondary)]/20 text-[var(--color-secondary)]"}`}>
              {step === "confirm" ? <span className="material-symbols-outlined text-sm">check</span> : "1"}
            </div>
            <div className="flex-1 h-px bg-[var(--color-outline-variant)]/30" />
            <div className={`w-8 h-8 rounded-full flex items-center justify-center text-xs font-bold ${step === "confirm" ? "bg-[var(--color-primary)] text-white" : "bg-[var(--color-surface-container)] text-[var(--color-outline)]"}`}>2</div>
          </div>

          <div className="mb-8">
            <h2 className="font-headline text-3xl font-extrabold text-[var(--color-on-surface)] tracking-tight mb-2">
              {step === "request" ? "Reset your password" : "Set a new password"}
            </h2>
            <p className="text-[var(--color-on-surface-variant)] text-sm">
              {step === "request"
                ? "Enter your work email to receive a reset code."
                : `Enter the 6-digit code sent to ${email} and choose a new password.`}
            </p>
          </div>

          {error && (
            <div className="mb-6 flex items-center gap-3 px-4 py-3 rounded-xl bg-red-50 text-red-600 text-sm font-medium">
              <span className="material-symbols-outlined text-base">error</span>
              {error}
            </div>
          )}

          {step === "request" ? (
            <form onSubmit={handleRequest} className="space-y-5">
              <div>
                <label className="block text-xs font-bold uppercase tracking-wider text-[var(--color-on-surface-variant)] mb-2">Work Email</label>
                <div className="relative">
                  <span className="material-symbols-outlined absolute left-4 top-1/2 -translate-y-1/2 text-[var(--color-outline)] text-xl">mail</span>
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    placeholder="name@company.com"
                    className="w-full pl-12 pr-4 py-3.5 rounded-xl bg-[var(--color-surface-container-low)] ring-1 ring-[var(--color-outline-variant)]/30 focus:ring-2 focus:ring-[var(--color-primary)] focus:outline-none text-[var(--color-on-surface)] placeholder:text-[var(--color-outline)]/50 text-sm transition-all"
                    required
                  />
                </div>
              </div>
              <button
                type="submit"
                disabled={loading}
                className="w-full py-4 rounded-xl btn-primary text-white font-headline font-bold text-sm shadow-lg shadow-[#00668a]/20 flex items-center justify-center gap-3 disabled:opacity-70"
              >
                {loading
                  ? <><span className="material-symbols-outlined animate-spin text-base">progress_activity</span>Sending code...</>
                  : <>Send Reset Code<span className="material-symbols-outlined text-base">send</span></>}
              </button>
            </form>
          ) : (
            <form onSubmit={handleConfirm} className="space-y-5">
              <div>
                <label className="block text-xs font-bold uppercase tracking-wider text-[var(--color-on-surface-variant)] mb-2">Verification Code</label>
                <div className="relative">
                  <span className="material-symbols-outlined absolute left-4 top-1/2 -translate-y-1/2 text-[var(--color-outline)] text-xl">pin</span>
                  <input
                    type="text"
                    inputMode="numeric"
                    maxLength={6}
                    value={code}
                    onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
                    placeholder="123456"
                    className="w-full pl-12 pr-4 py-3.5 rounded-xl bg-[var(--color-surface-container-low)] ring-1 ring-[var(--color-outline-variant)]/30 focus:ring-2 focus:ring-[var(--color-primary)] focus:outline-none text-[var(--color-on-surface)] placeholder:text-[var(--color-outline)]/50 text-sm tracking-[0.3em] transition-all"
                    required
                  />
                </div>
              </div>
              <div>
                <label className="block text-xs font-bold uppercase tracking-wider text-[var(--color-on-surface-variant)] mb-2">New Password</label>
                <div className="relative">
                  <span className="material-symbols-outlined absolute left-4 top-1/2 -translate-y-1/2 text-[var(--color-outline)] text-xl">lock</span>
                  <input
                    type={showPassword ? "text" : "password"}
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    placeholder="Min. 8 characters"
                    className="w-full pl-12 pr-12 py-3.5 rounded-xl bg-[var(--color-surface-container-low)] ring-1 ring-[var(--color-outline-variant)]/30 focus:ring-2 focus:ring-[var(--color-primary)] focus:outline-none text-[var(--color-on-surface)] placeholder:text-[var(--color-outline)]/50 text-sm transition-all"
                    required
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-4 top-1/2 -translate-y-1/2 text-[var(--color-outline)] hover:text-[var(--color-primary)] transition-colors"
                  >
                    <span className="material-symbols-outlined text-xl">{showPassword ? "visibility_off" : "visibility"}</span>
                  </button>
                </div>
              </div>
              <div>
                <label className="block text-xs font-bold uppercase tracking-wider text-[var(--color-on-surface-variant)] mb-2">Confirm New Password</label>
                <div className="relative">
                  <span className="material-symbols-outlined absolute left-4 top-1/2 -translate-y-1/2 text-[var(--color-outline)] text-xl">lock_reset</span>
                  <input
                    type={showPassword ? "text" : "password"}
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    placeholder="Repeat new password"
                    className="w-full pl-12 pr-4 py-3.5 rounded-xl bg-[var(--color-surface-container-low)] ring-1 ring-[var(--color-outline-variant)]/30 focus:ring-2 focus:ring-[var(--color-primary)] focus:outline-none text-[var(--color-on-surface)] placeholder:text-[var(--color-outline)]/50 text-sm transition-all"
                    required
                  />
                </div>
              </div>
              <button
                type="submit"
                disabled={loading}
                className="w-full py-4 rounded-xl btn-primary text-white font-headline font-bold text-sm shadow-lg shadow-[#00668a]/20 flex items-center justify-center gap-3 disabled:opacity-70"
              >
                {loading
                  ? <><span className="material-symbols-outlined animate-spin text-base">progress_activity</span>Resetting...</>
                  : <>Reset Password<span className="material-symbols-outlined text-base">check_circle</span></>}
              </button>
              <button
                type="button"
                onClick={() => setStep("request")}
                className="w-full text-sm text-[var(--color-on-surface-variant)] hover:text-[var(--color-primary)] transition-colors"
              >
                ← Back
              </button>
            </form>
          )}

          <p className="mt-8 text-center text-xs text-[var(--color-on-surface-variant)]">
            Remember your password?{" "}
            <a href="/" className="font-semibold text-[var(--color-primary)] hover:underline underline-offset-4">Sign In</a>
          </p>
          <p className="mt-4 text-center text-[10px] text-[var(--color-outline)]/50 uppercase tracking-widest">MetronAI v1.0 · Enterprise Edition</p>
        </div>
      </div>
    </div>
  );
}
