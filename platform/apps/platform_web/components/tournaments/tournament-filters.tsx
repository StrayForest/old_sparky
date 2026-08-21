"use client";

import { Calendar, ChevronDown, ListFilter, RefreshCcw, Search, Star } from "lucide-react";
import type { ChangeEvent } from "react";
import { useI18n } from "@/components/i18n-provider";
import { TournamentInviteClaim } from "@/components/tournaments/tournament-invite-claim";
import { ranks, tournamentStatuses } from "@/lib/tournament-model";
import type {
  TournamentDateSort,
  TournamentScope as TournamentScopeValue,
  TournamentStatus
} from "@/lib/types";

export type DateSort = TournamentDateSort;
export type TournamentScope = TournamentScopeValue;

export type TournamentFiltersValue = {
  search: string;
  scope: TournamentScope;
  status: TournamentStatus | "all";
  rank: string;
  dateSort: DateSort;
};

type TournamentFiltersProps = {
  value: TournamentFiltersValue;
  onChange: (value: TournamentFiltersValue) => void;
  onReset: () => void;
};

export function TournamentFilters({ value, onChange, onReset }: TournamentFiltersProps) {
  const { enumLabel, t } = useI18n();

  function update(next: Partial<TournamentFiltersValue>) {
    onChange({ ...value, ...next });
  }

  function updateSelect(
    event: ChangeEvent<HTMLSelectElement>,
    next: Partial<TournamentFiltersValue>
  ) {
    update(next);
    event.currentTarget.blur();
  }

  const dateSortLabel = value.dateSort === "nearest"
    ? t("tournaments.dateNearest")
    : value.dateSort === "farthest"
      ? t("tournaments.dateFarthest")
      : t("tournaments.dateSort");

  return (
    <div className="filters-panel" aria-label={t("tournaments.filters")}>
      <label className="filter-field">
        <span className="filter-control">
          <span className="left">
            <Search size={19} aria-hidden="true" />
            <input
              className="filter-input"
              data-testid="tournament-search-filter"
              maxLength={120}
              value={value.search}
              onChange={(event) => update({ search: event.target.value })}
              placeholder={t("tournaments.searchPlaceholder")}
            />
          </span>
        </span>
      </label>
      <TournamentInviteClaim />
      <label className="filter-field">
        <span className="filter-control select-control">
          <select
            className="filter-select"
            data-testid="status-filter"
            value={value.status}
            onChange={(event) => updateSelect(event, { status: event.target.value as TournamentStatus | "all" })}
            aria-label={t("tournaments.statusFilter")}
          >
            <option value="all">{t("tournaments.statusFilter")}</option>
            {tournamentStatuses.map((status) => (
              <option value={status.value} key={status.value}>{enumLabel(status.value)}</option>
            ))}
          </select>
          <ChevronDown size={17} aria-hidden="true" />
        </span>
      </label>
      <label className="filter-field">
        <span className="filter-control select-control has-leading-icon">
          <Star size={17} aria-hidden="true" />
          <select
            className="filter-select"
            data-testid="rank-filter"
            value={value.rank}
            onChange={(event) => updateSelect(event, { rank: event.target.value })}
            aria-label={t("tournaments.rankFilterShort")}
          >
            <option value="all">{t("tournaments.rankFilterShort")}</option>
            {ranks.map((rank) => <option value={rank.code} key={rank.code}>{rank.label}</option>)}
          </select>
          <ChevronDown size={17} aria-hidden="true" />
        </span>
      </label>
      <label className="filter-field">
        <span className="filter-control select-control has-leading-icon">
          <ListFilter size={17} aria-hidden="true" />
          <select
            className="filter-select"
            data-testid="tournament-scope-filter"
            value={value.scope}
            onChange={(event) => updateSelect(event, { scope: event.target.value as TournamentScope })}
            aria-label={t("tournament.scopeFilter")}
          >
            <option value="all">{t("tournament.scopeAll")}</option>
            <option value="mine">{t("tournament.scopeMine")}</option>
            <option value="registered">{t("tournament.scopeRegistered")}</option>
          </select>
          <ChevronDown size={17} aria-hidden="true" />
        </span>
      </label>
      <button
        aria-label={t("tournaments.dateSortAria")}
        className={value.dateSort === "none" ? "filter-control date-sort-button" : "filter-control date-sort-button active"}
        data-testid="date-sort-filter"
        type="button"
        onClick={(event) => {
          update({ dateSort: nextDateSort(value.dateSort) });
          event.currentTarget.blur();
        }}
      >
        <span className="left">
          <Calendar size={17} aria-hidden="true" />
          <span>{dateSortLabel}</span>
        </span>
      </button>
      <button className="reset-area" type="button" onClick={onReset}>
        <RefreshCcw size={18} aria-hidden="true" />
        {t("tournaments.resetFilters")}
      </button>
    </div>
  );
}

function nextDateSort(current: DateSort): DateSort {
  if (current === "none") {
    return "nearest";
  }
  if (current === "nearest") {
    return "farthest";
  }
  return "none";
}
