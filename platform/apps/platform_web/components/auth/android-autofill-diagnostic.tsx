"use client";

import { useEffect, useRef, useState } from "react";

export type AndroidAutofillDiagnosticMode = "signup" | "change";

type DiagnosticEntry = {
  id: number;
  at: string;
  event: string;
  field: string;
  length: number;
  inputType?: string;
  trusted?: boolean;
};

const observedEvents = ["focus", "beforeinput", "input", "change", "blur"] as const;
const diagnosticUsername = ["android-autofill-test", "example.invalid"].join("@");

export function AndroidAutofillDiagnostic({
  mode,
}: {
  mode: AndroidAutofillDiagnosticMode;
}) {
  const formRef = useRef<HTMLFormElement | null>(null);
  const nextEntryIdRef = useRef(1);
  const [entries, setEntries] = useState<DiagnosticEntry[]>([]);
  const [submitted, setSubmitted] = useState(false);

  useEffect(() => {
    const form = formRef.current;
    if (!form) {
      return;
    }

    const append = (
      event: string,
      input: HTMLInputElement,
      nativeEvent?: Event
    ) => {
      const inputEvent = nativeEvent instanceof InputEvent ? nativeEvent : null;
      const entry: DiagnosticEntry = {
        id: nextEntryIdRef.current++,
        at: new Date().toLocaleTimeString(),
        event,
        field: input.name || input.id || "unknown",
        length: input.value.length,
        inputType: inputEvent?.inputType || undefined,
        trusted: nativeEvent?.isTrusted,
      };
      setEntries((current) => [entry, ...current].slice(0, 80));
    };

    const nativeListeners = new Map<string, EventListener>();
    for (const eventName of observedEvents) {
      const listener: EventListener = (event) => {
        if (event.target instanceof HTMLInputElement) {
          append(eventName, event.target, event);
        }
      };
      nativeListeners.set(eventName, listener);
      form.addEventListener(eventName, listener, true);
    }

    const observedLengths = new Map<string, number>();
    for (const input of form.querySelectorAll<HTMLInputElement>("input")) {
      observedLengths.set(input.name || input.id, input.value.length);
    }

    const intervalId = window.setInterval(() => {
      for (const input of form.querySelectorAll<HTMLInputElement>("input")) {
        if (input.type !== "password") {
          continue;
        }
        const field = input.name || input.id;
        const previousLength = observedLengths.get(field);
        const nextLength = input.value.length;
        if (previousLength !== nextLength) {
          observedLengths.set(field, nextLength);
          append("dom-length-change", input);
        }
      }
    }, 200);

    return () => {
      window.clearInterval(intervalId);
      for (const [eventName, listener] of nativeListeners) {
        form.removeEventListener(eventName, listener, true);
      }
    };
  }, [mode]);

  function clearDiagnostic() {
    formRef.current?.reset();
    setEntries([]);
    setSubmitted(false);
  }

  return (
    <div className="auth-form" data-testid="android-autofill-diagnostic">
      <p className="description-text">
        Это локальная проверка Autofill. Значения полей и пароли не отправляются на сервер.
        Журнал ниже содержит только имя поля, тип события и длину значения.
      </p>
      <p className="description-text">
        Используется отдельный тестовый username, чтобы Google Password Manager не связывал
        проверку с вашим настоящим аккаунтом. Для проверки генерации пароль отправлять не нужно.
      </p>

      <form
        autoComplete="on"
        id={`android-autofill-${mode}-form`}
        method="post"
        name={`android-autofill-${mode}-form`}
        ref={formRef}
        onSubmit={(event) => {
          event.preventDefault();
          setSubmitted(true);
        }}
      >
        <label className="field" htmlFor="android-autofill-username">
          <span className="label">Email / username</span>
          <input
            autoCapitalize="none"
            autoComplete="username"
            className="input"
            defaultValue={diagnosticUsername}
            id="android-autofill-username"
            name="username"
            required
            spellCheck={false}
            type="text"
          />
        </label>

        {mode === "change" ? (
          <label className="field" htmlFor="android-autofill-current-password">
            <span className="label">Текущий пароль</span>
            <input
              autoComplete="current-password"
              className="input"
              id="android-autofill-current-password"
              maxLength={128}
              name="current-password"
              required
              type="password"
            />
          </label>
        ) : null}

        <label className="field" htmlFor="android-autofill-new-password">
          <span className="label">Новый пароль</span>
          <input
            autoComplete="new-password"
            className="input"
            id="android-autofill-new-password"
            maxLength={128}
            minLength={10}
            name="new-password"
            required
            type="password"
          />
        </label>

        <label className="field" htmlFor="android-autofill-confirm-password">
          <span className="label">Повторите новый пароль</span>
          <input
            autoComplete="new-password"
            className="input"
            id="android-autofill-confirm-password"
            maxLength={128}
            minLength={10}
            name="confirm-password"
            required
            type="password"
          />
        </label>

        <div className="auth-actions">
          <button className="primary-button" type="submit">
            Проверить submit
          </button>
          <button className="secondary-button" onClick={clearDiagnostic} type="button">
            Очистить
          </button>
        </div>
      </form>

      {submitted ? (
        <p aria-live="polite" className="auth-security-status" role="status">
          Submit перехвачен локально. На сервер ничего не отправлено.
        </p>
      ) : null}

      <div className="panel panel-pad" style={{ marginTop: 18 }}>
        <div className="panel-title-row">
          <h3 className="panel-title">Журнал Android Autofill</h3>
        </div>
        {entries.length ? (
          <ol
            data-testid="android-autofill-event-log"
            style={{ display: "grid", gap: 8, margin: 0, paddingLeft: 20 }}
          >
            {entries.map((entry) => (
              <li key={entry.id}>
                <code>
                  {entry.at} · {entry.field} · {entry.event} · length={entry.length}
                  {entry.inputType ? ` · inputType=${entry.inputType}` : ""}
                  {entry.trusted === undefined ? "" : ` · trusted=${String(entry.trusted)}`}
                </code>
              </li>
            ))}
          </ol>
        ) : (
          <p className="description-text" data-testid="android-autofill-empty-log">
            Событий пока нет. Нажмите на поле пароля и выберите предложение Google Password Manager.
          </p>
        )}
      </div>
    </div>
  );
}
