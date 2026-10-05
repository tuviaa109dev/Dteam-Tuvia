import { useState } from "react";
import HealthPanel from "./components/HealthPanel.jsx";
import SubmitJobForm from "./components/SubmitJobForm.jsx";
import JobsTable from "./components/JobsTable.jsx";
import JobDetail from "./components/JobDetail.jsx";
import DeadLetterDetail from "./components/DeadLetterDetail.jsx";
import DevPanel from "./components/DevPanel.jsx";

export default function App() {
  const [selectedId, setSelectedId] = useState(null); // job open in the job panel
  const [selectedDeadId, setSelectedDeadId] = useState(null); // dead letter open in the inspect panel
  const [refreshKey, setRefreshKey] = useState(0);
  const [statusFilter, setStatusFilter] = useState(""); // shared by the stat tiles and the table dropdown
  const [tab, setTab] = useState("jobs"); // "jobs" | "dlq" (dead letter queue)
  const bump = () => setRefreshKey((k) => k + 1);

  // Stat tiles: the dead letter tile opens the DLQ tab, every other tile filters the Jobs tab.
  const selectStat = (status) => {
    if (status === "dead_letter") {
      setTab("dlq");
    } else {
      setTab("jobs");
      setStatusFilter(status);
    }
  };

  // Only one side panel at a time.
  const openJob = (id) => {
    setSelectedDeadId(null);
    setSelectedId(id);
  };
  const openDeadLetter = (id) => {
    setSelectedId(null);
    setSelectedDeadId(id);
  };

  return (
    <div className="app">
      <header className="topbar">
        <h1>Job Queue</h1>
        <span className="muted">API → Redis queue → workers → PostgreSQL</span>
      </header>

      <HealthPanel
        refreshKey={refreshKey}
        activeStatus={tab === "dlq" ? "dead_letter" : statusFilter}
        onSelectStatus={selectStat}
      />

      <main className="layout">
        <div className="side">
          <section className="card">
            <h2>Submit a job</h2>
            <SubmitJobForm
              onSubmitted={(job) => {
                openJob(job.id);
                bump();
              }}
            />
          </section>
        </div>

        <div className="side grow">
          <section className="card jobs-card">
            <JobsTable
              tab={tab}
              onTabChange={setTab}
              refreshKey={refreshKey}
              selectedId={selectedId}
              onSelect={openJob}
              onChanged={bump}
              status={statusFilter}
              onStatusChange={setStatusFilter}
              selectedDeadLetterId={selectedDeadId}
              onSelectDeadLetter={openDeadLetter}
            />
          </section>
          {/* Dev tools are tucked away: they only appear while the Processing stat is selected. */}
          {tab === "jobs" && statusFilter === "processing" && (
            <DevPanel
              onFilled={bump}
              onCleared={() => {
                setSelectedId(null);
                setSelectedDeadId(null);
                bump();
              }}
            />
          )}
        </div>
      </main>

      {selectedId && (
        <JobDetail
          jobId={selectedId}
          onClose={() => setSelectedId(null)}
          onChanged={bump}
          onRerun={(copy) => openJob(copy.id)}
          onOpenDeadLetter={(id) => {
            setTab("dlq");
            openDeadLetter(id);
          }}
        />
      )}

      {selectedDeadId && (
        <DeadLetterDetail
          jobId={selectedDeadId}
          onClose={() => setSelectedDeadId(null)}
          onChanged={bump}
          onRequeued={(job) => {
            setTab("jobs");
            setStatusFilter("");
            openJob(job.id);
          }}
        />
      )}
    </div>
  );
}
