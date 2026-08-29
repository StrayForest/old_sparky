"use client";

import { useState } from "react";
import type { FormEvent } from "react";
import { KeyRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useI18n } from "@/components/i18n-provider";
import { platformApiMessage, platformApiRequest } from "@/lib/platform-api";

type InviteClaimResponse = { tournament: { slug: string } };

export function TournamentInviteGate({ slug }: { slug: string }) {
  const { t } = useI18n();
  const router = useRouter();
  const [code, setCode] = useState("");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const normalizedCode = code.trim().toUpperCase();
    if (!normalizedCode) return;
    setPending(true);
    setMessage("");
    try {
      await platformApiRequest<InviteClaimResponse>("/tournaments/invites/claim", {
        method: "POST",
        body: JSON.stringify({ code: normalizedCode, entry_type: "solo", team_name: null })
      });
      router.push(`/tournaments/${slug}?invite_code=${encodeURIComponent(normalizedCode)}`);
    } catch (error) {
      setMessage(platformApiMessage(error, t("tournaments.inviteClaimFailed")));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel panel-pad tournament-invite-gate">
      <KeyRound aria-hidden="true" size={24} />
      <h1>{t("tournament.visibilityInvite")}</h1>
      <p className="description-text">{t("tournament.inviteGateCopy")}</p>
      <form onSubmit={submit}>
        <label className="field">
          <span className="label">{t("tournaments.inviteCode")}</span>
          <input
            autoComplete="off"
            className="input"
            minLength={6}
            onChange={(event) => setCode(event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 24))}
            value={code}
          />
        </label>
        <button className="primary-action" disabled={pending || code.length < 6} type="submit">
          {pending ? t("common.loading") : t("tournament.redeemInvite")}
        </button>
      </form>
      {message ? <p className="form-message error" role="alert">{message}</p> : null}
    </section>
  );
}
