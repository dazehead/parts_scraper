import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
import datetime as dt
import threading
import pandas as pd
import math
from database import Database
from statedb import StateDB
from math import ceil
import io
import time
import os
import sys
from helpers import Helper
from batch_watermark_detector import BatchWatermarkDetector
from report_builder import ReportBuilder, RunSummary, new_job_id
from obs import get_logger
from tenancy.ids import validate_tenant_id, InvalidTenantError, MissingTenantError
from dotenv import load_dotenv
load_dotenv()

_log = get_logger("operator.gui")


class PartsScraperGUI:
    def __init__(self, root, testing=False):
        self.testing = testing
        self.root = root
        self.root.title("-Parts Scraper-")
        self.root.geometry("850x700")
        self.root.resizable(True, True)
        
        # Constants
        self.open_ai_key = os.getenv("OPENAI_API_KEY")
        self.bucket = os.getenv("BUCKET")
        self.search_job_key = os.getenv("SEARCH_KEY")
        self.process_job_key = os.getenv("PROC_KEY")
        self.search_queue = os.getenv("SEARCH_QUEUE_URL")
        self.process_queue = os.getenv("PROC_QUEUE_URL")
        self.search_chunk_size = 5
        self.processing_chunk_size = 10
        self.max_rows = 25

        # Tests
        self.test_input_key = os.getenv("TEST_KEY")
        self.test_queue = os.getenv("TEST_QUEUE_URL")

        # Variables
        self.csv_file_path = tk.StringVar()
        self.processing = False
        self.csv_loaded = False
        self.total_rows = 0
        self.dataframe = None

        # Tenant id for the current run. Defaults to $DEFAULT_TENANT_ID
        # so single-tenant deployments don't need to think about it.
        self.tenant_id = tk.StringVar(value=os.getenv("DEFAULT_TENANT_ID", ""))

        # Setup GUI
        self.create_widgets()

        self.state = StateDB()
        self.run_summary = self._restore_or_init_summary()

        # Build db/detector/helper bound to the current tenant. If the
        # operator changes the tenant via the dropdown, we rebuild them.
        self.db = None
        self.detector = None
        self.helper = None
        self.reporter = ReportBuilder(bucket=self.bucket) if self.bucket else None
        self._rebind_tenant(self.tenant_id.get())

        self.determine_state()

    # ----- tenant binding ----------------------------------------------
    def _rebind_tenant(self, tenant_id):
        """(Re-)build tenant-bound clients (db, detector, helper).

        Called once at init and again whenever the operator switches
        tenant from the GUI. Safe to call with an empty string — the
        detector/helper are simply left as None and the search /
        watermark / filter buttons remain disabled.
        """
        try:
            tenant_id = validate_tenant_id(tenant_id) if tenant_id else None
        except (InvalidTenantError, MissingTenantError) as e:
            self.log_message(f"Invalid tenant id: {e}")
            return

        self.db = Database(tenant_id=tenant_id)
        self.detector = BatchWatermarkDetector(self.db, self.open_ai_key)
        self.helper = Helper(self.db, self.detector, tenant_id=tenant_id)
        if tenant_id:
            self.run_summary.tenant_id = tenant_id
            self._persist_summary()
            self.log_message(f"Active tenant: {tenant_id}")
        else:
            self.log_message("No tenant selected. Pick one before submitting work.")

    # ----- run summary plumbing -----------------------------------------
    def _restore_or_init_summary(self):
        current = self.state.read()
        raw = current.get("run_summary") if isinstance(current, dict) else None
        if raw and isinstance(raw, dict) and raw.get("job_id"):
            return RunSummary(
                job_id=raw["job_id"],
                customer=raw.get("customer") or os.getenv("CUSTOMER", "unknown"),
                tenant_id=raw.get("tenant_id") or os.getenv("DEFAULT_TENANT_ID", "unknown"),
                started_at=dt.datetime.fromisoformat(raw["started_at"]),
                finished_at=(
                    dt.datetime.fromisoformat(raw["finished_at"])
                    if raw.get("finished_at") else None
                ),
                csv_rows=raw.get("csv_rows", 0),
                parts_with_existing_image=raw.get("parts_with_existing_image", 0),
                parts_searched=raw.get("parts_searched", 0),
                candidates_downloaded=raw.get("candidates_downloaded", 0),
                candidates_flagged=raw.get("candidates_flagged", 0),
                candidates_accepted=raw.get("candidates_accepted", 0),
                final_images_written=raw.get("final_images_written", 0),
                batches_total=raw.get("batches_total", 0),
                batches_unusable=list(raw.get("batches_unusable", [])),
                notes=list(raw.get("notes", [])),
            )
        # Fresh summary; not yet persisted.
        return RunSummary(
            job_id=new_job_id(),
            customer=os.getenv("CUSTOMER", "unknown"),
            tenant_id=os.getenv("DEFAULT_TENANT_ID", "unknown"),
            started_at=dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
        )

    def _persist_summary(self):
        d = self.run_summary.as_dict()
        # _persist_summary stores in state.json next to other run flags.
        self.state.set(run_summary=d)


    def perform_watermark(self):
        self.state.set(image_search_state=False)
        self.progress_bar.start(30)
        self.status_var.set("Performing AI watermark detection")
        self.log_message("Performing AI watermark detection")
        self.ai_watermark_btn.configure(state=tk.DISABLED)

        ids = self.helper.organize_and_submit_batch()
        self.run_summary.batches_total += len(ids or [])
        self._persist_summary()
        self.log_message("Data has been sent to AI - waiting for response")
        self.state.set(batch_ids=ids)
        self.poll_open_ai()

        # Count flagged candidates from staged delete keys before they get cleared.
        flagged_now = len(getattr(self.db, "delete_keys", []) or [])
        self.run_summary.candidates_flagged += flagged_now
        # Accepted = downloaded - flagged so far. Bounded at 0 in case of drift.
        self.run_summary.candidates_accepted = max(
            0,
            self.run_summary.candidates_downloaded - self.run_summary.candidates_flagged,
        )
        self._persist_summary()

        self.db.send_delete_request_watermark()

        self.progress_bar.stop()
        self.log_message("Ready for last step Filter Images")
        self.filter_images_btn.configure(state=tk.NORMAL)


    def poll_open_ai(self):
        current = self.state.read()
        batch_ids = current.get("batch_ids")

        completed = False
        count = 0
        all_results = []
        time.sleep(40)

        while not completed:
            time.sleep(60)
            count += 1
            self.clear_log()
            self.log_message("Data has been sent to AI - Could take anywhere from 5 minutes to 12 hours to process")
            self.log_message(f"minutes: {count}")
            for batch_id in batch_ids:
                result, status = self.detector.poll_multiple_batch_completion(batch_id)
                self.log_message(f"{batch_id} : {status}")
                all_results.append(result)
            if all(all_results):
                completed = True
            all_results = []
            self.log_message('\n')
        
        self.log_message("AI has determined which images have watermarks and they are being deleted...")

        try:
            self.helper.parse_ai_results(batch_ids)
        except Exception as e:
            # Failed/expired batches are now surfaced explicitly. Capture
            # them on the run summary so the report makes the gap visible
            # to the operator and the customer.
            from batch_watermark_detector import BatchUnusableError
            if isinstance(e, BatchUnusableError):
                _log.error("openai batches unusable", error=str(e))
                # Re-derive the list of (batch_id, status) from the message.
                for token in str(e).split(":", 1)[-1].split(","):
                    token = token.strip()
                    if "(" in token and token.endswith(")"):
                        bid, status = token[:-1].split("(", 1)
                        self.run_summary.batches_unusable.append(
                            {"batch_id": bid.strip(), "status": status.strip()}
                        )
                self.run_summary.notes.append(
                    "Some OpenAI batches finished unusable; classifier did not run for those candidates."
                )
                self._persist_summary()
                # Don't auto-advance to the filter stage; the operator
                # decides whether to re-submit or accept the gap.
                self.log_message(str(e))
                raise
            raise
        self.state.set(image_watermark_detection=True)
    def perform_filter(self): # this is process
        self.progress_bar.start(30)
        self.filter_images_btn.configure(state=tk.DISABLED)
        self.status_var.set("Performing Hash Similiarity")

        self.log_message("Gathering Remaining Images...")
        
        all_data = self.db.read_sql_query(
            "SELECT tag_value FROM dbo.part_tags "
            "WHERE tenant_id = :tenant_id ORDER BY tag_value ASC;",
            params={"tenant_id": self.run_summary.tenant_id},
        )

        self.log_message("Splitting groups into jobs...")
        num_chunks = self.helper.split_group_upload(
            df=all_data,
            bucket=self.bucket,
            prefix=self.process_job_key,
            chunk_size = self.processing_chunk_size
        )

        self.clear_log()
        start = time.time()
        if self.testing:
            self.helper.send_chunk_messages(   
                job_id = "Testing",           
                queue_url = self.test_queue,   
                num_chunks =  10,               
                key = self.test_input_key)      
        else:
            self.helper.send_chunk_messages(  
                job_id = "process_images",          
                queue_url = self.process_queue,   
                num_chunks =  num_chunks,            
                key = self.process_job_key)   
    

        self.log_message("Instances being created in the Cloud Please Wait...")

        all_terminated = False
        count = 0
        time.sleep(70)
        while not all_terminated:
            time.sleep(60)
            all_terminated,state = self.helper.determine_instance_state()
            count += 1
            self.clear_log()
            self.log_message("Program will be complete after processing...")
            self.log_message(f"\nProcessing Images...\n\tStatus: {state}\n\tMinutes: {count}")
        
        end = time.time()
        print(f"Elapsed Time for filter : {end - start}")
        # Deletes the CSV files from search_jobs
        self.db.empty_prefix(self.bucket, self.process_job_key)

        self.clear_log()
        self.log_message("All images have been processed")
        self.search_images_btn.configure(state=tk.DISABLED)
        self.progress_bar.stop()

        # Count final images written during this run window, scoped to the active tenant.
        try:
            row = self.db.read_sql_query(
                "SELECT COUNT(*) AS n FROM dbo.parts "
                "WHERE tenant_id = :tenant_id AND final_tag IS NOT NULL;",
                params={"tenant_id": self.run_summary.tenant_id},
            )
            self.run_summary.final_images_written = int(row["n"].iat[0])
        except Exception as e:
            _log.warning("could not count final images", error=str(e))

        # Build a small sample of representative results for the HTML.
        samples = []
        try:
            sample_rows = self.db.read_sql_query(
                "SELECT TOP 12 number, description, final_tag "
                "FROM dbo.parts WHERE tenant_id = :tenant_id AND final_tag IS NOT NULL "
                "ORDER BY part_id DESC;",
                params={"tenant_id": self.run_summary.tenant_id},
            )
            samples = [
                {
                    "part_number": str(r["number"]),
                    "description": str(r.get("description", "") or ""),
                    "final_url": str(r["final_tag"]),
                }
                for _, r in sample_rows.iterrows()
            ]
        except Exception as e:
            _log.warning("could not pull report samples", error=str(e))

        # Ship the report. Don't block completion on a report failure.
        if self.reporter is not None:
            try:
                import datetime as _dt
                self.run_summary.finished_at = _dt.datetime.utcnow()
                refs = self.reporter.write(self.run_summary, samples=samples)
                self._persist_summary()
                self.log_message(f"Run report: {refs['html_url']}")
                _log.info("run report written", **refs, job_id=self.run_summary.job_id)
            except Exception as e:
                _log.warning("report generation failed", error=str(e))
                self.log_message(f"Report generation failed: {e}")

        # self.db.update_all_final_tags() handling this in image process class
        self.db.execute_sql(
            "DELETE FROM dbo.part_tags WHERE tenant_id = :tenant_id;",
            params={"tenant_id": self.run_summary.tenant_id},
        )
        self.db.empty_prefix(self.bucket, 'images')
        self.status_var.set("COMPLETED: Images are Ready for Deployment")
        self.state.set(image_watermark_detection=False)
        # Clear the run_summary so the next CSV starts a fresh job_id.
        self.state.set(run_summary=None)
        self.run_summary = self._restore_or_init_summary()

    def search_images(self):
        """Main processing function."""
        if not self.helper or not self.helper.tenant_id:
            messagebox.showerror(
                "Tenant required",
                "Set a tenant id and click Apply before starting an image search.",
            )
            return

        # Tenant-registry gate: refuses to start a run if the tenant
        # is suspended/archived or would blow its monthly quota.
        try:
            from tenancy import TenantRegistry
            registry = TenantRegistry(self.db)
            would_add = int(len(self.dataframe))
            ok, reason = registry.check_quota(self.helper.tenant_id, would_add=would_add)
            if not ok:
                messagebox.showerror("Tenant gate", f"Refusing to start: {reason}")
                _log.error("tenant gate refused run", tenant_id=self.helper.tenant_id, reason=reason)
                return
            self.log_message(f"Tenant gate: {reason}")
        except Exception as e:
            # Registry table is optional (migration 004); a missing table
            # shouldn't break single-tenant deployments that haven't run
            # it yet.
            _log.warning("tenant registry check skipped", error=str(e))

        self.status_var.set("Performing Image Search...")
        self.progress_bar.start(30)
        self.log_message(f"Uploading CSV to Database for tenant {self.helper.tenant_id}...")
        # Database.upsert_append_new_only auto-stamps tenant_id from
        # the Database's tenant when the DataFrame lacks the column.
        self.db.upsert_append_new_only(self.dataframe, target="dbo.parts")

        # Stamp this run.
        self.run_summary.csv_rows = int(len(self.dataframe))
        self._persist_summary()
        _log.info(
            "search stage starting",
            job_id=self.run_summary.job_id,
            csv_rows=self.run_summary.csv_rows,
        )

        # retrieving data from database (tenant-scoped)
        self.log_message('Gathering all parts without an image...')
        all_data = self.db.read_sql_query(
            "SELECT number, description FROM dbo.parts "
            "WHERE tenant_id = :tenant_id AND final_tag IS NULL;",
            params={"tenant_id": self.run_summary.tenant_id},
        )
        all_data = all_data.iloc[:self.max_rows]
        self.run_summary.parts_searched = int(len(all_data))
        self.run_summary.parts_with_existing_image = max(
            0, self.run_summary.csv_rows - self.run_summary.parts_searched
        )
        self._persist_summary()

        # spliting data into chucks and upload to s3
        self.log_message("splitting data into jobs...")
        num_chunks = self.helper.split_data_and_upload_jobs(
            df=all_data,
            bucket=self.bucket,
            prefix=self.search_job_key,
            chunk_size = self.search_chunk_size,
            testing=self.testing) 
        
        self.clear_log()
        self.state.set(image_search_state=False)
        
        start = time.time()   #####################################
        if self.testing:
            self.helper.send_chunk_messages(   
                job_id = "Testing",           
                queue_url = self.test_queue,  
                num_chunks =  10,              
                key = self.test_input_key)     
        else:
            self.helper.send_chunk_messages(  
                job_id = "image search",          
                queue_url = self.search_queue,   
                num_chunks =  num_chunks,          
                key = self.search_job_key)        


        self.log_message("Watermark Button will become clickable when all images have been downloaded...")
        self.log_message(f"{num_chunks} Instances being created in cloud please wait...")

        
        all_terminated = False
        count = 0
        time.sleep(70)
        while not all_terminated:
            time.sleep(60)
            all_terminated,state = self.helper.determine_instance_state()
            count += 1
            self.clear_log()
            self.log_message("Watermark Button will become clickable when all images have been downloaded...")
            self.log_message(f"\nStill downloading images...\n\tStatus: {state}\n\tMinutes: {count}")
        


        end = time.time()
        self.state.set(image_search_state=True)
        _log.info("search stage complete", elapsed_seconds=int(end - start))

        # Count the candidates the workers actually persisted, scoped to tenant.
        try:
            row = self.db.read_sql_query(
                "SELECT COUNT(*) AS n FROM dbo.part_tags WHERE tenant_id = :tenant_id;",
                params={"tenant_id": self.run_summary.tenant_id},
            )
            self.run_summary.candidates_downloaded = int(row["n"].iat[0])
            self._persist_summary()
        except Exception as e:
            _log.warning("could not count downloaded candidates", error=str(e))

        # Deletes the CSV files from search_jobs
        self.db.empty_prefix(self.bucket, self.search_job_key)

        self.clear_log()
        self.log_message("All images have been downloaded.\nYou can now start the watermark")
        self.ai_watermark_btn.configure(state=tk.NORMAL)
        self.search_images_btn.configure(state=tk.DISABLED)
        self.progress_bar.stop()

    def determine_state(self):
        current = self.state.read()
        if current.get("image_search_state"):
            self.log_message("Images Search has been performed next step is for AI watermark")
            self.toggle_controls(False, True)
            self.ai_watermark_btn.configure(state=tk.NORMAL)

        elif current.get("image_watermark_detection"):
            self.log_message("Waiting for completion of AI Watermark detector via Cloud will update when finished")
            self.toggle_controls(False, True)
            self.filter_images_btn.configure(state=tk.NORMAL)

        elif current.get("image_hashing"):
            self.log_message("Image Hashing is talking place via Cloud will update whne finished")
            self.toggle_controls(False, True)
        else:
            self.log_message("No jobs yet performed")
            self.toggle_controls(False)



    def toggle_controls(self, enabled, full_lock=False):
        """Enable/disable all controls except CSV browse button"""
        state = tk.NORMAL if enabled else tk.DISABLED
        
        # Processing options
        for widget in self.options_frame.winfo_children():
            if isinstance(widget, ttk.Checkbutton):
                widget.config(state=state)
        if full_lock:
            # needs to set CSV to disable
            self.csv_browse_btn.configure(state=tk.DISABLED)
            
        
        # Process button
        if enabled and self.csv_loaded:
            self.search_images_btn.config(state=tk.NORMAL)
        else:
            self.search_images_btn.config(state=tk.DISABLED)

        
        
    def create_widgets(self):
        # Main frame
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        

        # Configure grid weights
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        main_frame.columnconfigure(1, weight=1)
        

        # Title
        title_label = ttk.Label(main_frame, text="Parts Image Scraper", 
                               font=('Arial', 16, 'bold'))
        title_label.grid(row=0, column=0, columnspan=3, pady=(0, 20))
        

        # CSV File Selection
        ttk.Label(main_frame, text="CSV File:").grid(row=1, column=0, sticky=tk.W, pady=5)
        
        csv_frame = ttk.Frame(main_frame)
        csv_frame.grid(row=1, column=1, columnspan=2, sticky=(tk.W, tk.E), pady=5)
        csv_frame.columnconfigure(0, weight=1)
        
        self.csv_entry = ttk.Entry(csv_frame, textvariable=self.csv_file_path, width=60)
        self.csv_entry.grid(row=0, column=0, sticky=(tk.W, tk.E), padx=(0, 5))
        
        self.csv_browse_btn = ttk.Button(csv_frame, text="Browse", command=self.browse_csv)
        self.csv_browse_btn.grid(row=0, column=1)
        

        # CSV Info Label
        self.csv_info_var = tk.StringVar(value="No CSV file loaded")
        self.csv_info_label = ttk.Label(main_frame, textvariable=self.csv_info_var,
                                       font=('Arial', 9), foreground='gray')
        self.csv_info_label.grid(row=2, column=0, columnspan=3, sticky=tk.W, pady=(0, 10))

        # Tenant selector. Free-form entry so the operator can switch to a
        # tenant that the dropdown doesn't list yet.
        tenant_frame = ttk.Frame(main_frame)
        tenant_frame.grid(row=3, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=(0, 8))
        ttk.Label(tenant_frame, text="Tenant:").grid(row=0, column=0, padx=(0, 5))
        self.tenant_entry = ttk.Entry(tenant_frame, textvariable=self.tenant_id, width=24)
        self.tenant_entry.grid(row=0, column=1, sticky=tk.W)
        self.tenant_apply_btn = ttk.Button(
            tenant_frame, text="Apply",
            command=lambda: self._rebind_tenant(self.tenant_id.get().strip()),
        )
        self.tenant_apply_btn.grid(row=0, column=2, padx=(8, 0))
          

        # Processing Options
        self.options_frame = ttk.LabelFrame(main_frame, text="Processing Options", padding="10")
        self.options_frame.grid(row=5, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=10)
        self.options_frame.columnconfigure(1, weight=1)


        # Progress Bar
        # self.progress_var = tk.DoubleVar()
        # self.progress_bar = ttk.Progressbar(main_frame, variable=self.progress_var, 
        #                                    maximum=100, length=400)
        # self.progress_bar.grid(row=7, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=10)

        self.progress_bar = ttk.Progressbar(main_frame, mode=['indeterminate'], 
                                           length=400)
        self.progress_bar.grid(row=7, column=0, columnspan=3, sticky=(tk.W, tk.E), pady=10)
        

        # Status Label
        self.status_var = tk.StringVar(value="Please load a CSV file to begin...")
        self.status_label = ttk.Label(main_frame, textvariable=self.status_var)
        self.status_label.grid(row=8, column=0, columnspan=3, pady=5)
        

        # Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=9, column=0, columnspan=3, pady=20)
        
        self.search_images_btn = ttk.Button(button_frame, text="Start Image Search", 
                                     command=self.start_search, style="Accent.TButton")
        self.search_images_btn.pack(side=tk.LEFT, padx=5)

        self.ai_watermark_btn = ttk.Button(button_frame, text='AI Watermark',
                                           command=self.start_watermark, style='Accent.TButton', state=tk.DISABLED)  
        self.ai_watermark_btn.pack(side=tk.LEFT, padx=5)

        self.filter_images_btn = ttk.Button(button_frame, text="Filter Images",
                                            command=self.start_filter, style='Accent.TButton', state=tk.DISABLED)
        self.filter_images_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(button_frame, text="Stop", 
                                  command=self.stop_processing, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=5)
        
        clear_btn = ttk.Button(button_frame, text="Clear Log", command=self.clear_log)
        clear_btn.pack(side=tk.RIGHT, padx=5)



        # Log Text Area
        log_frame = ttk.LabelFrame(main_frame, text="Processing Log", padding="5")
        log_frame.grid(row=10, column=0, columnspan=3, sticky=(tk.W, tk.E, tk.N, tk.S), pady=10)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        main_frame.rowconfigure(10, weight=1)
        
        self.log_text = ScrolledText(log_frame, height=12, width=80)
        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        
    def update_instance_info(self, value=None):
        """Update instance information label"""
        instances = int(self.instance_count.get())
        if self.csv_loaded:
            images_per_instance = math.ceil(self.total_rows / instances)
            info_text = f"{instances} instance{'s' if instances != 1 else ''} ({images_per_instance} images per instance)"
        else:
            info_text = f"{instances} instance{'s' if instances != 1 else ''}"
        self.instance_info_var.set(info_text)
        
    def validate_csv_structure(self, df):
        """Validate CSV structure and return validation result"""
        required_columns = ['number', 'description']
        missing_columns = [col for col in required_columns if col not in df.columns]
        
        if missing_columns:
            return False, f"Missing required columns: {', '.join(missing_columns)}"

        total_paths = len(df)
        
        return True, f"Valid CSV with {total_paths} rows."
        
    def browse_csv(self):
        """Browse for CSV file"""
        self.file_path = filedialog.askopenfilename(
            title="Select CSV File",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
        )
        if self.file_path:
            self.csv_file_path.set(self.file_path)
            self.load_csv_info(self.file_path)

        # Enable controls
        self.csv_loaded = True
        self.toggle_controls(True)
        self.status_var.set("Ready to process...")
        self.log_message(f"CSV loaded: {self.total_rows} images")

            
    def load_csv_info(self, csv_path):
        """Load CSV file and update UI accordingly"""
        try:
            # Read CSV
            self.dataframe = pd.read_csv(csv_path, sep=',', header=0, index_col=0)
            self.total_rows = len(self.dataframe)
            
            # Validate CSV structure
            is_valid, message = self.validate_csv_structure(self.dataframe)
            
            if not is_valid:
                messagebox.showerror("Invalid CSV", message)
                self.csv_loaded = False
                self.csv_info_var.set("Invalid CSV file")
                self.toggle_controls(False)
                return
            
            # Update CSV info
            self.csv_info_var.set(f"✓ {message}")
            self.clear_log()
            self.log_message('\nHere is a view of the data.')
            self.log_message(self.dataframe.head(5).to_string())
            self.log_message('..........................................')
            self.log_message('..........................................')
            self.log_message(self.dataframe.tail(5).to_string())


        except Exception as e:
            messagebox.showerror("Error", f"Failed to load CSV file:\n{str(e)}")
            self.csv_loaded = False
            self.csv_info_var.set("Failed to load CSV")
            self.toggle_controls(False)
            self.log_message(f"Error loading CSV: {str(e)}")
            
            
    def log_message(self, message):
        """Add message to log"""
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()
        
    def clear_log(self):
        """Clear the log"""
        self.log_text.delete(1.0, tk.END)
        
    def update_progress(self, value, status=""):
        """Update progress bar and status"""
        self.progress_var.set(value)
        if status:
            self.status_var.set(status)
        self.root.update_idletasks()

    def start_filter(self):
        self.clear_log()
        self.log_message("Starting filter process of remaining images...")
        self.start_threading(self.perform_filter)

    def start_watermark(self):
        self.clear_log()
        self.log_message('Sending data to AI for watermark detection...')
        self.start_threading(self.perform_watermark)

    def start_search(self):
        """Start processing in a separate thread"""
        if not self.csv_loaded:
            messagebox.showerror("Error", "Please load a valid CSV file first")
            return
        result = messagebox.askyesno("Confirm", "This will upload the parts to the Database and start Cloud Instances to Process all the images.\nIf the part and number is already in the database it will not be duplicated. \n\nMAKE SURE THE DATA IS CORRECT BEFORE CHOOSING YES.", default='no')
        if result:
            
            self.processing = True
            self.toggle_controls(False)
            self.stop_btn.config(state=tk.NORMAL)
            #self.progress_var.set(0)


            # Start processing thread
            self.clear_log()
            self.log_message("Starting to Process...")

            self.start_threading(self.search_images)
        else:
            pass

    def start_threading(self, function, params=None):
        if params is not None:
            threading.Thread(target=function(params), daemon=True).start()

        else:
            threading.Thread(target=function, daemon=True).start()

                            
        
    def stop_processing(self):
        """Stop processing"""
        self.processing = False
        self.status_var.set("Stopping...")
        self.log_message("Processing stopped by user")
        self.progress_bar.stop()


def main():
    root = tk.Tk()
    
    # Set theme (optional)
    try:
        root.tk.call("source", "azure.tcl")
        root.tk.call("set_theme", "light")
    except:
        pass  # Theme not available
        
    app = PartsScraperGUI(root, testing=False)
    
    # Center window
    root.update_idletasks()
    x = (root.winfo_screenwidth() // 2) - (root.winfo_width() // 2)
    y = (root.winfo_screenheight() // 2) - (root.winfo_height() // 2)
    root.geometry(f"+{x}+{y}")
    
    root.mainloop()


if __name__ == "__main__":
    main()