type PageErrorScriptResponse = {
  url: string;
  status: number;
  resourceType: string;
};

type PageErrorAttachmentInput = {
  pageUrl: string;
  errors: Error[];
  successfulScriptResponses: PageErrorScriptResponse[];
  truncated?: boolean;
};

type PageErrorAttachment = {
  name: "live-pageerror-evidence-v1";
  contentType: "application/json";
  body: Buffer;
};

type UserApiOperation =
  | "tournament_create"
  | "tournament_status"
  | "invite_create"
  | "invite_claim"
  | "tournament_join"
  | "ready_check_start"
  | "ready_check_vote"
  | "ready_check_close"
  | "captain_round_start"
  | "assignment_start"
  | "assignment_state"
  | "assignment_publish"
  | "assignment_lock"
  | "bracket_read"
  | "match_schedule"
  | "tournament_read";

type UserApiFailureKind = "unexpected_status" | "invalid_json" | "request_error";
type UserApiOperationContract = {
  readonly method: "GET" | "POST" | "PATCH";
  readonly expected_status: number;
  readonly path_pattern: RegExp;
};
type UserApiFailureInput = {
  kind: UserApiFailureKind;
  operation: UserApiOperation;
  method: "GET" | "POST" | "PATCH";
  expected_status: number;
  actual_status: number | null;
  source_file: "tests/smoke/live-user-journey.spec.ts";
  source_line: number;
  source_column: number;
};
type UserApiFailureAttachment = {
  name: "live-user-api-failure-v1";
  contentType: "application/json";
  body: Buffer;
};

declare class LiveCountReporter {
  constructor(options?: { gateRoot?: string; sourceRoot?: string });
}

declare namespace LiveCountReporter {
  function createPageErrorAttachment(
    input: PageErrorAttachmentInput,
  ): PageErrorAttachment | null;
  function trustedSuccessfulNextScriptPath(
    response: PageErrorScriptResponse,
  ): string | null;
  function pageErrorClass(error: Error): string;
  function createLiveUserApiFailureAttachment(
    input: UserApiFailureInput,
  ): UserApiFailureAttachment | null;
  function userApiFailureMessage(kind: UserApiFailureKind): string;
  function matchesLiveUserApiOperation(
    operation: UserApiOperation,
    method: string,
    requestPath: string,
  ): boolean;
  const USER_API_OPERATION_CONTRACTS: Readonly<Record<UserApiOperation, UserApiOperationContract>>;
  const MAX_TRACKED_NEXT_SCRIPTS: number;
  const MAX_PAGE_ERROR_EVENTS: number;
}

export = LiveCountReporter;
