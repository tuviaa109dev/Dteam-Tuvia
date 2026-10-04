import { useState } from "react";
import HealthPanel from "./components/HealthPanel.jsx";
import SubmitJobForm from "./components/SubmitJobForm.jsx";
import JobsTable from "./components/JobsTable.jsx";
import JobDetail from "./components/JobDetail.jsx";
import DevPanel from "./components/DevPanel.jsx";

export default function App() {
  const [selectedId, setSelectedId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const bump = () => setRefreshKey((k) => k + 1);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Job Queue</h1>
        <span className="muted">API → Redis queue → workers → PostgreSQL</span>
      </header>

      <HealthPanel refreshKey={refreshKey} />

      <main className="layout">
        <div className="side">
          <section className="card">
            <h2>Submit a job</h2>
            <SubmitJobForm
              onSubmitted={(job) => {
                setSelectedId(job.id);
                bump();
              }}
            />
          </section>
          <DevPanel
            onFilled={bump}
            onCleared={() => {
              setSelectedId(null);
              bump();
            }}
          />
        </div>

        <section className="card grow">
          <JobsTable refreshKey={refreshKey} selectedId={selectedId} onSelect={setSelectedId} onChanged={bump} />
        </section>
      </main>

      {selectedId && (
        <JobDetail
          jobId={selectedId}
          onClose={() => setSelectedId(null)}
          onChanged={bump}
          onRerun={(copy) => setSelectedId(copy.id)}
        />
      )}
    </div>
  );
}
