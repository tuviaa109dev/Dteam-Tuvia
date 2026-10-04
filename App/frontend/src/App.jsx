import { useState } from "react";
import HealthPanel from "./components/HealthPanel.jsx";
import SubmitJobForm from "./components/SubmitJobForm.jsx";
import JobsTable from "./components/JobsTable.jsx";
import JobDetail from "./components/JobDetail.jsx";

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
        <section className="card">
          <h2>Submit a job</h2>
          <SubmitJobForm
            onSubmitted={(job) => {
              setSelectedId(job.id);
              bump();
            }}
          />
        </section>

        <section className="card grow">
          <JobsTable refreshKey={refreshKey} selectedId={selectedId} onSelect={setSelectedId} />
        </section>
      </main>

      {selectedId && (
        <JobDetail jobId={selectedId} onClose={() => setSelectedId(null)} onChanged={bump} />
      )}
    </div>
  );
}
