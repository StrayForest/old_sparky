"use client";

import { AlertTriangle, Search, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useAuth } from "@/components/auth/auth-provider";
import { platformApiMessage, platformApiRequest } from "@/lib/platform-api";
import type { PlatformUser } from "@/lib/platform-types";

export function AdminUserDeletion() {
  const { user: currentUser } = useAuth();
  const [search, setSearch] = useState("");
  const [users, setUsers] = useState<PlatformUser[]>([]);
  const [selectedUserId, setSelectedUserId] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [note, setNote] = useState("");
  const [isSearching, setIsSearching] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const isSuperadmin = currentUser?.roles.includes("superadmin") === true;
  const selected = useMemo(
    () => users.find((user) => user.id === selectedUserId) ?? null,
    [selectedUserId, users]
  );
  const expectedConfirmation = selected ? (selected.email ?? selected.id) : "";
  const canDelete = Boolean(
    isSuperadmin
    && selected
    && selected.id !== currentUser?.id
    && !selected.roles.includes("superadmin")
    && confirmation.trim() === expectedConfirmation
    && note.trim().length >= 3
    && !isDeleting
  );

  useEffect(() => {
    if (!isSuperadmin) {
      return;
    }
    const query = search.trim();
    if (query.length < 2) {
      setUsers([]);
      setSelectedUserId("");
      return;
    }
    const timeout = window.setTimeout(async () => {
      setIsSearching(true);
      setError("");
      try {
        const found = await platformApiRequest<PlatformUser[]>(
          `/admin/users?search=${encodeURIComponent(query)}`
        );
        setUsers(found);
        setSelectedUserId((current) => (
          current && found.some((item) => item.id === current)
            ? current
            : found[0]?.id ?? ""
        ));
      } catch (requestError) {
        setUsers([]);
        setSelectedUserId("");
        setError(platformApiMessage(requestError, "Не удалось найти пользователя."));
      } finally {
        setIsSearching(false);
      }
    }, 300);
    return () => window.clearTimeout(timeout);
  }, [isSuperadmin, search]);

  useEffect(() => {
    setConfirmation("");
    setNote("");
    setMessage("");
    setError("");
  }, [selectedUserId]);

  if (!isSuperadmin) {
    return null;
  }

  async function deleteUser() {
    if (!selected || !canDelete) {
      return;
    }
    setIsDeleting(true);
    setMessage("");
    setError("");
    try {
      await platformApiRequest<void>(`/admin/users/${selected.id}`, {
        method: "DELETE",
        body: JSON.stringify({
          confirmation: confirmation.trim(),
          note: note.trim()
        })
      });
      setMessage(`Аккаунт ${selected.display_name} удалён из базы данных.`);
      setUsers((current) => current.filter((item) => item.id !== selected.id));
      setSelectedUserId("");
      setConfirmation("");
      setNote("");
      setSearch("");
    } catch (requestError) {
      setError(platformApiMessage(requestError, "Не удалось удалить аккаунт."));
    } finally {
      setIsDeleting(false);
    }
  }

  return (
    <section className="admin-console" data-testid="admin-user-deletion-console">
      <header className="admin-console-header">
        <div>
          <div className="admin-eyebrow"><Trash2 size={16} />Удаление аккаунта</div>
          <h2>Удалить пользователя из БД</h2>
          <p>Операция доступна только superadmin и необратимо удаляет аккаунт и связанные каскадные данные.</p>
        </div>
      </header>

      <div className="admin-section">
        <div className="admin-toolbar admin-users-toolbar">
          <label className="admin-search">
            <Search size={18} />
            <input
              data-testid="admin-delete-user-search"
              maxLength={120}
              value={search}
              placeholder="Ник или почта пользователя"
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <div className="admin-result-count">
            {isSearching ? "Поиск..." : search.trim().length < 2 ? "Введите минимум 2 символа" : `Найдено: ${users.length}`}
          </div>
        </div>

        <div className="admin-workspace">
          <div className="admin-table-panel">
            <div className="admin-table-scroll">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Пользователь</th>
                    <th>Роли</th>
                    <th>Статус</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((user) => (
                    <tr
                      className={selectedUserId === user.id ? "selected admin-clickable-row" : "admin-clickable-row"}
                      key={user.id}
                      tabIndex={0}
                      onClick={() => setSelectedUserId(user.id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelectedUserId(user.id);
                        }
                      }}
                    >
                      <td>
                        <div className="admin-record-button">
                          <strong>{user.display_name}</strong>
                          <span>{user.email ?? user.id}</span>
                        </div>
                      </td>
                      <td><div className="admin-role-list">{user.roles.map((role) => <span key={role}>{role}</span>)}</div></td>
                      <td>{user.status}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {error && !selected ? <div className="admin-feedback error">{error}</div> : null}
          </div>

          <aside className="admin-inspector">
            {!selected ? (
              <div className="admin-empty">Найдите и выберите пользователя.</div>
            ) : (
              <>
                <div className="admin-inspector-head">
                  <div>
                    <div className="admin-inspector-kicker">Удаление аккаунта</div>
                    <h2>{selected.display_name}</h2>
                    <p>{selected.email ?? selected.id}</p>
                  </div>
                  <div className="admin-user-avatar">{selected.display_name.slice(0, 2).toUpperCase()}</div>
                </div>

                <div className="admin-form-section admin-danger-zone">
                  <div className="admin-callout danger">
                    <AlertTriangle size={17} />
                    <span>
                      Удаление необратимо. Если пользователь владеет турнирами или у него есть активные медиа-файлы,
                      сервер остановит операцию и сообщит, что нужно удалить сначала.
                    </span>
                  </div>

                  {selected.id === currentUser?.id ? (
                    <div className="admin-feedback error">Собственный аккаунт удалить здесь нельзя.</div>
                  ) : null}
                  {selected.roles.includes("superadmin") ? (
                    <div className="admin-feedback error">Аккаунты superadmin защищены от удаления.</div>
                  ) : null}

                  <label className="admin-note-field">
                    <span>Для подтверждения введите точно</span>
                    <input
                      data-testid="admin-delete-user-confirmation"
                      maxLength={320}
                      value={confirmation}
                      onChange={(event) => setConfirmation(event.target.value)}
                    />
                    <small>{expectedConfirmation}</small>
                  </label>

                  <label className="admin-note-field">
                    <span>Причина удаления</span>
                    <textarea
                      data-testid="admin-delete-user-note"
                      maxLength={1000}
                      value={note}
                      onChange={(event) => setNote(event.target.value)}
                    />
                  </label>

                  {message ? <div className="admin-feedback success">{message}</div> : null}
                  {error ? <div className="admin-feedback error">{error}</div> : null}

                  <button
                    className="secondary-button admin-apply-button danger"
                    data-testid="admin-delete-user"
                    disabled={!canDelete}
                    type="button"
                    onClick={() => void deleteUser()}
                  >
                    <Trash2 size={18} />
                    {isDeleting ? "Удаление..." : "Удалить аккаунт из БД"}
                  </button>
                </div>
              </>
            )}
          </aside>
        </div>
      </div>
    </section>
  );
}
