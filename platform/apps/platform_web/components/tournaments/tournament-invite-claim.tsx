"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { KeyRound } from "lucide-react";
import { useI18n } from "@/components/i18n-provider";
import { platformApiMessage, platformApiRequest } from "@/lib/platform-api";

type InviteClaimResponse = {
  tournament: {
    slug: string;
    name: string;
  };
};

export function TournamentInviteClaim() {
  const { t } = useI18n();
  const router = useRouter();
  const [code, setCode] = useState("");
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");
  const requestSequence = useRef(0);

  useEffect(() => {
    const normalizedCode = code.trim().toUpperCase();
    const sequence = ++requestSequence.current;
    if (normalizedCode.length < 6) {
      setPending(false);
      setMessage("");
      return undefined;
    }

    const controller = new AbortController();
    const timeout = window.setTimeout(() => {
      setPending(true);
      setMessage("");
      void platformApiRequest<InviteClaimResponse>("/tournaments/invites/claim", {
        method: "POST",
        body: JSON.stringify({ code: normalizedCode, entry_type: "solo", team_name: null }),
        signal: controller.signal
      })
        .then((result) => {
          if (requestSequence.current === sequence) {
            router.push(`/tournaments/${result.tournament.slug}`);
          }
        })
        .catch((error) => {
          if (!controller.signal.aborted && requestSequence.current === sequence) {
            setMessage(platformApiMessage(error, t("tournaments.inviteClaimFailed")));
          }
        })
        .finally(() => {
          if (requestSequence.current === sequence) {
            setPending(false);
          }
        });
    }, 650);

    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [code, router, t]);

  return (
    <label className={message ? "filter-field invite-filter-field invalid" : "filter-field invite-filter-field"}>
      <span className="filter-control">
        <span className="left">
          <KeyRound aria-hidden="true" size={18} />
        <input
          autoComplete="off"
          aria-label={t("tournaments.inviteCode")}
          className="filter-input invite-filter-input"
          data-testid="tournament-invite-code"
          id="tournament-invite-code"
          maxLength={24}
          minLength={6}
          onChange={(event) => setCode(event.target.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 24))}
          placeholder={t("tournaments.invitePlaceholder")}
          value={code}
        />
        </span>
        {pending ? <span className="invite-filter-pending" aria-hidden="true" /> : null}
      </span>
      {message ? <span className="invite-filter-message" role="alert">{message}</span> : null}
    </label>
  );
}
