"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { TournamentCard } from "@/components/tournaments/tournament-card";
import { TournamentFilters, type TournamentFiltersValue } from "@/components/tournaments/tournament-filters";
import { useI18n } from "@/components/i18n-provider";
import {
  getMyTournamentSummaries,
  getTournamentSummaries,
  PlatformApiError
} from "@/lib/platform-api";
import type { TournamentListQuery, TournamentPage, TournamentSummary } from "@/lib/types";

const defaultFilters: TournamentFiltersValue = {
  search: "",
  scope: "all",
  status: "all",
  rank: "all",
  dateSort: "none"
};
const tournamentsPerPage = 9;

type TournamentListClientProps = {
  initialPage: TournamentPage;
};

type TournamentPageLoader = (signal: AbortSignal) => Promise<TournamentPage>;

type CachedTournamentPage = {
  page: TournamentPage;
  expiresAt: number;
};

type InFlightTournamentPage = {
  controller: AbortController;
  consumers: number;
  cancelled: boolean;
  settled: boolean;
  promise: Promise<TournamentPage>;
};

class TournamentPageQueryCache {
  private readonly cache = new Map<string, CachedTournamentPage>();
  private readonly inFlight = new Map<string, InFlightTournamentPage>();
  private static readonly maxEntries = 32;

  constructor(private readonly ttlMs: number) {}

  prime(key: string, page: TournamentPage): void {
    this.setCached(key, page);
  }

  get(
    key: string,
    loader: TournamentPageLoader,
    signal: AbortSignal,
    cacheResult: boolean,
  ): Promise<TournamentPage> {
    if (signal.aborted) {
      return Promise.reject(abortError());
    }

    if (cacheResult) {
      const cached = this.cache.get(key);
      if (cached && cached.expiresAt > Date.now()) {
        return Promise.resolve(cached.page);
      }
      this.cache.delete(key);
    }

    let request = this.inFlight.get(key);
    if (!request) {
      const controller = new AbortController();
      const promise = loader(controller.signal);
      const newRequest: InFlightTournamentPage = {
        controller,
        consumers: 0,
        cancelled: false,
        settled: false,
        promise,
      };
      request = newRequest;
      this.inFlight.set(key, newRequest);
      void promise.then(
        () => this.finish(key, newRequest),
        () => this.finish(key, newRequest),
      );
      if (cacheResult) {
        void promise.then((page) => {
          if (!newRequest.cancelled) {
            this.setCached(key, page);
          }
        });
      }
    }

    const activeRequest = request;
    activeRequest.consumers += 1;
    return new Promise<TournamentPage>((resolve, reject) => {
      let consumed = false;
      const release = () => {
        if (consumed) {
          return;
        }
        consumed = true;
        activeRequest.consumers -= 1;
        if (activeRequest.consumers === 0 && !activeRequest.settled) {
          activeRequest.cancelled = true;
          activeRequest.controller.abort();
        }
      };
      const onAbort = () => {
        release();
        reject(abortError());
      };
      signal.addEventListener("abort", onAbort, { once: true });
      activeRequest.promise.then(
        (page) => {
          if (consumed) {
            return;
          }
          release();
          signal.removeEventListener("abort", onAbort);
          resolve(page);
        },
        (error: unknown) => {
          if (consumed) {
            return;
          }
          release();
          signal.removeEventListener("abort", onAbort);
          reject(error);
        },
      );
    });
  }

  private finish(key: string, request: InFlightTournamentPage): void {
    request.settled = true;
    if (this.inFlight.get(key) === request) {
      this.inFlight.delete(key);
    }
  }

  private setCached(key: string, page: TournamentPage): void {
    const now = Date.now();
    for (const [cachedKey, cached] of this.cache) {
      if (cached.expiresAt <= now) {
        this.cache.delete(cachedKey);
      }
    }
    while (this.cache.size >= TournamentPageQueryCache.maxEntries) {
      const oldestKey = this.cache.keys().next().value;
      if (!oldestKey) {
        break;
      }
      this.cache.delete(oldestKey);
    }
    this.cache.set(key, { page, expiresAt: now + this.ttlMs });
  }
}

function abortError(): Error {
  return new Error("Tournament page request was aborted");
}

const tournamentPageCacheTtlMs = 15_000;

export function TournamentListClient({ initialPage }: TournamentListClientProps) {
  const { t } = useI18n();
  const [filters, setFilters] = useState<TournamentFiltersValue>(defaultFilters);
  const [debouncedSearch, setDebouncedSearch] = useState(defaultFilters.search);
  const [page, setPage] = useState<TournamentPage>(initialPage);
  const [resolvedQueryKey, setResolvedQueryKey] = useState(() => queryKey(defaultFilters));
  const [isLoading, setIsLoading] = useState(false);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pageLoadFailed, setPageLoadFailed] = useState(false);
  const activeQueryKey = useRef(resolvedQueryKey);
  const loadMoreControllerRef = useRef<AbortController | null>(null);
  const loadMoreSentinelRef = useRef<HTMLDivElement | null>(null);
  const pageCacheRef = useRef<TournamentPageQueryCache | null>(null);
  if (pageCacheRef.current === null) {
    pageCacheRef.current = new TournamentPageQueryCache(tournamentPageCacheTtlMs);
    pageCacheRef.current.prime(
      tournamentPageQueryKey("all", { limit: tournamentsPerPage }),
      initialPage,
    );
  }
  const pageCache = pageCacheRef.current;

  useEffect(() => {
    const timeout = window.setTimeout(() => setDebouncedSearch(filters.search), 300);
    return () => window.clearTimeout(timeout);
  }, [filters.search]);

  const requestQuery = useMemo(
    () => buildRequestQuery(
      filters.scope,
      filters.status,
      filters.rank,
      filters.dateSort,
      debouncedSearch,
    ),
    [debouncedSearch, filters.dateSort, filters.rank, filters.scope, filters.status]
  );
  const requestedQueryKey = useMemo(
    () => queryKey({ ...filters, search: debouncedSearch }),
    [debouncedSearch, filters]
  );
  activeQueryKey.current = requestedQueryKey;

  useEffect(() => {
    if (requestedQueryKey === resolvedQueryKey) {
      setIsLoading(false);
      setIsLoadingMore(false);
      setError(null);
      setPageLoadFailed(false);
      return;
    }

    loadMoreControllerRef.current?.abort();
    loadMoreControllerRef.current = null;

    const controller = new AbortController();
    setIsLoading(true);
    setIsLoadingMore(false);
    setError(null);
    setPageLoadFailed(false);

    void requestTournamentPage(
      filters.scope,
      requestQuery,
      controller.signal,
      pageCache,
    )
      .then((nextPage) => {
        if (activeQueryKey.current !== requestedQueryKey) {
          return;
        }
        setPage(nextPage);
        setPageLoadFailed(false);
        setResolvedQueryKey(requestedQueryKey);
      })
      .catch((requestError: unknown) => {
        if (controller.signal.aborted || activeQueryKey.current !== requestedQueryKey) {
          return;
        }
        setError(requestError instanceof PlatformApiError && requestError.status === 401
          ? t("tournaments.signInForMine")
          : t("tournaments.loadFailed"));
        setPageLoadFailed(true);
      })
      .finally(() => {
        if (activeQueryKey.current === requestedQueryKey) {
          setIsLoading(false);
        }
      });

    return () => controller.abort();
  }, [filters.scope, requestQuery, requestedQueryKey, resolvedQueryKey, t]);

  useEffect(() => () => {
    loadMoreControllerRef.current?.abort();
  }, []);

  const loadMore = useCallback(async () => {
    if (!page.hasMore || isLoading || isLoadingMore || loadMoreControllerRef.current) {
      return;
    }

    const keyAtRequest = requestedQueryKey;
    const controller = new AbortController();
    loadMoreControllerRef.current = controller;
    setIsLoadingMore(true);
    setError(null);
    try {
      const nextPage = await requestTournamentPage(
        filters.scope,
        { ...requestQuery, cursor: page.nextCursor ?? undefined },
        controller.signal,
        pageCache,
      );
      if (activeQueryKey.current !== keyAtRequest) {
        return;
      }
      setPage((current) => ({
        ...nextPage,
        items: appendUnique(current.items, nextPage.items)
      }));
    } catch (requestError) {
      if (controller.signal.aborted) {
        return;
      }
      if (activeQueryKey.current === keyAtRequest) {
        setError(requestError instanceof PlatformApiError && requestError.status === 401
          ? t("tournaments.signInForMine")
          : t("tournaments.loadFailed"));
      }
    } finally {
      if (loadMoreControllerRef.current === controller) {
        loadMoreControllerRef.current = null;
      }
      if (activeQueryKey.current === keyAtRequest) {
        setIsLoadingMore(false);
      }
    }
  }, [filters.scope, isLoading, isLoadingMore, page.hasMore, page.nextCursor, pageCache, requestQuery, requestedQueryKey, t]);

  useEffect(() => {
    const sentinel = loadMoreSentinelRef.current;
    if (!sentinel || !page.hasMore || pageLoadFailed) {
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          void loadMore();
        }
      },
      { rootMargin: "240px 0px" }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [loadMore, page.hasMore, pageLoadFailed]);

  const emptyMessage = filters.scope === "registered"
    ? t("tournaments.registeredEmpty")
    : filters.scope === "mine"
      ? t("tournaments.myEmpty")
      : t("tournaments.empty");
  const visibleItems = page.items;
  const hasEmptyResults = !isLoading && !pageLoadFailed && visibleItems.length === 0;

  return (
    <>
      <TournamentFilters value={filters} onChange={setFilters} onReset={() => setFilters(defaultFilters)} />

      <section
        className={hasEmptyResults ? "tournaments-grid tournaments-grid-empty" : "tournaments-grid"}
        aria-busy={isLoading || isLoadingMore}
        aria-label={t("tournaments.listAria")}
        data-testid="tournaments-grid"
      >
        {!isLoading && !pageLoadFailed ? visibleItems.map((tournament) => (
          <TournamentCard tournament={tournament} key={tournament.id} />
        )) : null}
        {isLoading ? (
          <div className="empty-state tournament-list-state tournament-list-loading" role="status">
            {t("tournaments.loading")}
          </div>
        ) : null}
        {!isLoading && (pageLoadFailed || visibleItems.length === 0) ? (
          <div className="empty-state tournament-list-state" role={pageLoadFailed ? "alert" : "status"}>
            {pageLoadFailed ? error : emptyMessage}
          </div>
        ) : null}
      </section>

      {error && !pageLoadFailed && visibleItems.length > 0 ? (
        <div className="tournament-list-error" role="alert">{error}</div>
      ) : null}

      <div className="bottom-row">
        <div className="shown-count" data-testid="shown-count">
          {t("tournaments.showingCount", {
            count: pageLoadFailed ? 0 : visibleItems.length,
          })}
        </div>
        <div
          aria-hidden="true"
          className="tournaments-load-sentinel"
          data-testid="tournaments-load-sentinel"
          ref={loadMoreSentinelRef}
        />
      </div>
    </>
  );
}

function buildRequestQuery(
  scope: TournamentFiltersValue["scope"],
  status: TournamentFiltersValue["status"],
  rank: string,
  dateSort: TournamentFiltersValue["dateSort"],
  search: string
): TournamentListQuery {
  return {
    search: search.trim() || undefined,
    scope: scope === "all" ? undefined : scope,
    status: status === "all" ? undefined : status,
    rank: rank === "all" ? undefined : rank,
    dateSort: dateSort === "none" ? undefined : dateSort,
    limit: tournamentsPerPage
  };
}

function requestTournamentPage(
  scope: TournamentFiltersValue["scope"],
  query: TournamentListQuery,
  signal: AbortSignal,
  pageCache: TournamentPageQueryCache,
): Promise<TournamentPage> {
  const key = tournamentPageQueryKey(scope, query);
  return pageCache.get(
    key,
    (requestSignal) => scope === "all"
      ? getTournamentSummaries(query, { signal: requestSignal })
      : getMyTournamentSummaries(query, { signal: requestSignal }),
    signal,
    scope === "all",
  );
}

function tournamentPageQueryKey(
  scope: TournamentFiltersValue["scope"],
  query: TournamentListQuery,
): string {
  return JSON.stringify({
    scope,
    search: query.search?.trim() ?? "",
    status: query.status ?? "",
    rank: query.rank ?? "",
    dateSort: query.dateSort ?? "",
    limit: query.limit ?? tournamentsPerPage,
    cursor: query.cursor ?? "",
  });
}

function appendUnique(
  current: TournamentSummary[],
  incoming: TournamentSummary[]
): TournamentSummary[] {
  const seen = new Set(current.map((tournament) => tournament.id));
  const appended = [...current];
  for (const tournament of incoming) {
    if (!seen.has(tournament.id)) {
      seen.add(tournament.id);
      appended.push(tournament);
    }
  }
  return appended;
}

function queryKey(filters: TournamentFiltersValue): string {
  return JSON.stringify({
    search: filters.search.trim(),
    scope: filters.scope,
    status: filters.status,
    rank: filters.rank,
    dateSort: filters.dateSort
  });
}
