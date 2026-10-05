export default function Pager({ offset, total, page, onChange }) {
  if (total <= page) return null;
  return (
    <div className="pager">
      <button disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - page))}>Previous</button>
      <span className="muted">{offset + 1}–{Math.min(offset + page, total)} of {total}</span>
      <button disabled={offset + page >= total} onClick={() => onChange(offset + page)}>Next</button>
    </div>
  );
}
