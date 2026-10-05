import { useEffect, useState } from "react";
import { api } from "../api.js";
import { usePolling } from "../usePolling.js";
import ConfirmDialog from "./ConfirmDialog.jsx";
import Pager from "./Pager.jsx";
import { formatTime } from "./StatusBadge.jsx";

const PAGE = 25;
const COLUMNS = 8;

/** The dead letter queue: jobs whose data can never be processed, kept apart for inspection. */
export default function DeadLetterTable({ type, refreshKey, selectedId, onSelect, onChanged, onTotal }) {
  const [offset, setOffset] = useState(0);
  const [confirm, setConfirm] = useState(null); // { id } to discard one, { all: true } to purge
  const [actionError, setActionError] = useState(null);

  useEffect(() => setOffset(0), [type]);

  const { data, error, refresh } = usePolling(
    () => api.listDeadLetters({ type, limit: PAGE, offset }),
    2000,
    [type, offset, refreshKey]
  );
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  useEffect(() => onTotal(data ? total : null), [data, total, onTotal]);

  const discard = async () => {
    const target = confirm;
    setConfirm(null);
    setActionError(null);
    try {
      if (target.all) await api.purgeDeadLetters(type);
      else await api.discardDeadLetter(target.id);
      if (target.id === selectedId) onSelect(null);
      await refresh();
      onChanged();
    } catch (err) {
      setActionError(err.message);
    }
  };

  return (
    <>
      <p className="muted dlq-intro">
        Jobs whose data can never be processed (unknown type or invalid payload). They were moved out of the jobs
        list on their first attempt. Open one to inspect it, fix its payload and requeue it, or discard it.
      </p>

      {(error || actionError) && <div className="notice notice-error">{error || actionError}</div>}

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th><th>Type</th><th>Error type</th><th>Error</th><th>Attempts</th>
              <th>Created</th><th>Dead-lettered</th><th>Action</th>
            </tr>
          </thead>
          <tbody>
            {items.map((entry) => (
              <tr key={entry.id} className={entry.id === selectedId ? "selected" : ""} onClick={() => onSelect(entry.id)}>
                <td className="mono">{entry.id.slice(0, 8)}</td>
                <td>{entry.type}</td>
                <td><span className="badge badge-dead_letter">{entry.error_type}</span></td>
                <td className="cell-error" title={entry.error}>{entry.error}</td>
                <td className="num">{entry.attempts}/{entry.max_attempts}</td>
                <td>{formatTime(entry.created_at)}</td>
                <td>{formatTime(entry.dead_lettered_at)}</td>
                <td>
                  <button
                    className="row-action row-action-danger"
                    onClick={(e) => {
                      e.stopPropagation();
                      setConfirm({ id: entry.id });
                    }}
                  >
                    Discard
                  </button>
                </td>
              </tr>
            ))}
            {data && items.length === 0 && (
              <tr><td colSpan={COLUMNS} className="empty">The dead letter queue is empty.</td></tr>
            )}
          </tbody>
          {total > 1 && (
            <tfoot>
              <tr>
                <td colSpan={COLUMNS - 1} />
                <td>
                  <button className="danger-solid bulk-action" onClick={() => setConfirm({ all: true })}>Discard all</button>
                </td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>

      <Pager offset={offset} total={total} page={PAGE} onChange={setOffset} />

      {confirm && (
        <ConfirmDialog
          title={confirm.all ? "Discard all dead letters?" : "Discard this dead letter?"}
          message={
            confirm.all
              ? `Are you sure you want to permanently delete all ${total} ${type ? `${type} ` : ""}dead letters? Their data and logs cannot be recovered.`
              : "Are you sure you want to permanently delete this job? Its data and logs cannot be recovered."
          }
          confirmLabel={confirm.all ? "Yes, discard all" : "Yes, discard"}
          danger
          onConfirm={discard}
          onCancel={() => setConfirm(null)}
        />
      )}
    </>
  );
}
