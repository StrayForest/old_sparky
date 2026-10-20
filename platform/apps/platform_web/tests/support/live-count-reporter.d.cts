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
  const MAX_TRACKED_NEXT_SCRIPTS: number;
  const MAX_PAGE_ERROR_EVENTS: number;
}

export = LiveCountReporter;
