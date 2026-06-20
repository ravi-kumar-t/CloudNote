# CloudNote

CloudNote is an automated virtual classroom attendance platform built using Python and Selenium.

The project automatically logs into the university learning portal, detects upcoming classes, joins sessions at the correct time, configures microphone settings, confirms audio setup, and remains connected throughout the class duration.

The long-term vision is to evolve CloudNote from a single-user browser automation script into a cloud-native attendance automation platform that can securely manage multiple users, schedule attendance workflows, and operate entirely from cloud infrastructure.

---

## Why I Built This

During online classes, students often forget to join sessions on time, face connectivity issues, or need to keep their devices running throughout the lecture.

I built CloudNote to automate the repetitive process of joining classes and managing attendance while exploring browser automation, scheduling systems, cloud-native architectures, and DevOps practices.

---

## Features

### Current Features

* Automatic login to university portal
* Detection of scheduled classes
* Auto-join virtual classrooms
* Automatic microphone selection
* Automatic audio confirmation
* Countdown timer handling
* Browser permission handling
* Session persistence throughout class duration
* Error recovery and retry logic

### Planned Features

* Multi-user support
* Session management
* Cloud-hosted attendance automation
* Secure account onboarding
* Redis-based job scheduling
* Monitoring and observability dashboards
* Kubernetes-based scaling

---

## Tech Stack

### Current Implementation

* Python
* Selenium WebDriver
* Chrome Browser Automation
* WebDriver Manager

### Planned Cloud-Native Architecture

* FastAPI
* PostgreSQL
* Redis
* Docker
* Kubernetes
* GitHub Actions
* Prometheus
* Grafana
* Terraform

---

## Project Workflow

```text
Start Script
     ↓
Login to Portal
     ↓
Open Class Calendar
     ↓
Detect Scheduled Class
     ↓
Read Countdown Timer
     ↓
Wait Until Join Available
     ↓
Click Join
     ↓
Switch to Meeting Frame
     ↓
Select Microphone
     ↓
Confirm Audio
     ↓
Join Classroom
     ↓
Stay Connected
     ↓
Exit After Class Ends
```

---

## Long-Term Architecture Vision

```text
Student Portal
       │
       ▼
FastAPI Backend
       │
       ▼
PostgreSQL Database
       │
       ▼
Redis Job Queue
       │
       ▼
Attendance Workers
       │
       ▼
Selenium Automation Containers
       │
       ▼
University Learning Portal
```

---

## Installation

Clone the repository:

```bash
git clone https://github.com/ravi-kumar-t/CloudNote.git
cd CloudNote
```

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Configuration

Update the following values inside the script:

```python
USERNAME = "your_registration_number"
PASSWORD = "your_password"
```

---

## Run

```bash
python auto_join_class.py
```

---

## Future Roadmap

### Phase 1 ✅

* Local automation using Selenium
* Automatic classroom joining
* Microphone handling
* Session persistence

### Phase 2 🚧

* Multi-user support
* Session management

### Phase 3 🚧

* FastAPI backend
* PostgreSQL database

### Phase 4 🚧

* Redis job queue

### Phase 5 🚧

* Docker containerization

### Phase 6 🚧

* Cloud deployment

### Phase 7 🚧

* Kubernetes orchestration

### Phase 8 🚧

* Monitoring and observability

  * Prometheus
  * Grafana
  * Centralized logging

### Phase 9 🚧

* Infrastructure as Code

  * Terraform
  * Automated provisioning

---

## Project Structure

```text
.
├── auto_join_class.py
├── requirements.txt
├── README.md
├── screenshots/
└── docs/
```

---

## Current Status

CloudNote is currently under active development.

Implemented:

* Selenium-based attendance automation
* Automatic login and class detection
* Classroom joining workflow
* Microphone and audio handling
* Error recovery mechanisms

Planned:

* Cloud-native backend services
* Multi-user architecture
* Containerized deployment
* Kubernetes orchestration
* Monitoring and observability stack

---

## Disclaimer

This project was developed for educational and research purposes to explore browser automation, scheduling systems, cloud-native architectures, and DevOps workflows.

Users are responsible for ensuring compliance with their institution's policies and platform terms of service.

---

## Author

Ravi Kumar Tekkali

GitHub: https://github.com/ravi-kumar-t

LinkedIn: https://linkedin.com/in/ravikumar-tekkali
