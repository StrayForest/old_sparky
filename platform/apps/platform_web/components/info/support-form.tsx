"use client";

import type { FormEvent } from "react";
import { useState } from "react";
import { useI18n } from "@/components/i18n-provider";
import { platformApiMessage } from "@/lib/platform-api";
import { submitSupportMessage, type SupportCategory } from "@/lib/support-api";

const categories: SupportCategory[] = ["account", "tournament", "technical", "rules", "other"];

type SupportFormProps = {
  supportConfigured: boolean;
};

export function SupportForm({ supportConfigured }: SupportFormProps) {
  const { t } = useI18n();
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [category, setCategory] = useState<SupportCategory>("tournament");
  const [message, setMessage] = useState("");
  const [website, setWebsite] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  async function submitSupport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSubmitting(true);
    setFeedback(null);
    try {
      await submitSupportMessage({ name, email, category, message, website });
      setMessage("");
      setFeedback({ kind: "ok", text: t("info.supportSent") });
    } catch (error) {
      setFeedback({
        kind: "error",
        text: platformApiMessage(error, t("info.supportFailed"))
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form className="support-form" onSubmit={(event) => void submitSupport(event)}>
      <div className="support-form-grid">
        <label><span>{t("info.supportName")}</span><input maxLength={80} minLength={2} onChange={(event) => setName(event.target.value)} required value={name} /></label>
        <label><span>{t("info.supportEmail")}</span><input maxLength={254} onChange={(event) => setEmail(event.target.value)} required type="email" value={email} /></label>
      </div>
      <label><span>{t("info.supportCategory")}</span><select onChange={(event) => setCategory(event.target.value as SupportCategory)} value={category}>{categories.map((value) => <option key={value} value={value}>{t(`info.supportCategory.${value}`)}</option>)}</select></label>
      <label className="support-message-field"><span>{t("info.supportMessage")}</span><textarea maxLength={1000} minLength={10} onChange={(event) => setMessage(event.target.value)} required rows={8} value={message} /><small>{message.length}/1000</small></label>
      <label className="support-honeypot" aria-hidden="true"><span>Website</span><input autoComplete="off" onChange={(event) => setWebsite(event.target.value)} tabIndex={-1} value={website} /></label>
      {!supportConfigured ? <p className="support-unavailable" role="status">{t("info.supportUnavailable")}</p> : null}
      {feedback ? <p className={`support-feedback ${feedback.kind}`} role="status">{feedback.text}</p> : null}
      <button className="primary-button" disabled={!supportConfigured || submitting} type="submit">{submitting ? t("info.supportSending") : t("info.supportSubmit")}</button>
      <p className="support-privacy">{t("info.supportPrivacy")}</p>
    </form>
  );
}
