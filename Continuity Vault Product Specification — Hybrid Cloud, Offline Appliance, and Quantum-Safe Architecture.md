# Continuity Vault Product Specification  
## Hybrid Public Cloud, Managed Offline Appliance, and Quantum-Safe Architecture

**Document version:** 2.0  
**Status:** Production architecture baseline  
**Product:** Continuity Vault  
**Primary deployment models:** Public cloud vault, customer-controlled cloud vault, and cloud-managed offline hardware appliance  
**Security baseline:** Post-quantum-ready hybrid cryptography, immutable storage, separated trust domains, and zero-standing-access administration

---

# 1. Product Definition

Continuity Vault is a cloud-managed digital continuity and cyber-recovery platform that backs up, preserves, verifies, and restores critical information from:

- Email services
- Password managers
- Cloud file-storage services
- Collaboration platforms
- Local computers and servers
- Network-attached storage
- Mobile-device exports
- Identity and legal-document repositories
- SaaS applications
- Customer-defined data sources

Customers may store protected data in one or more of the following destinations:

1. **Continuity Vault Public Cloud**
2. **Customer-Owned Public Cloud**
3. **Continuity Vault Offline Appliance**
4. **Hybrid Cloud and Offline Appliance**
5. **Cloud-to-Appliance-to-Secondary-Site configuration**

The cloud layer provides centralized policy, orchestration, connector management, health monitoring, audit, identity, inventory, and recovery workflow management.

The protected recovery data may remain:

- In vendor-managed public cloud storage
- In a customer-controlled cloud account
- On an offline appliance at a customer-controlled location
- On an appliance in a Continuity Vault-managed secure facility
- Across multiple destinations under one protection policy

The platform must clearly separate:

- **Control plane:** Manages policy, jobs, identity, health, approvals, and audit.
- **Data plane:** Moves encrypted backup and restore data.
- **Recovery plane:** Stores immutable, recoverable copies.
- **Key plane:** Creates, wraps, releases, rotates, and destroys cryptographic keys.
- **Management plane:** Monitors and administers appliances without requiring continuous access to protected data.

---

# 2. Architectural Principles

## 2.1 Cloud managed does not mean cloud accessible

The cloud service may manage the state and lifecycle of an offline appliance without having continuous access to its stored data.

Management operations and content-access operations must be technically separated.

The cloud may normally see:

- Appliance identity
- Hardware health
- Software version
- Storage utilization
- Backup-job status
- Snapshot identifiers
- Verification status
- Environmental telemetry
- Network state
- Security events
- Signed inventory summaries

The cloud must not normally see:

- Plaintext customer files
- Plaintext email content
- Password-vault secrets
- Unwrapped collection keys
- Appliance recovery keys
- Decrypted indexes
- Customer master keys

## 2.2 Offline means inaccessible through the network

An appliance must not be marketed as offline merely because its storage is in a separate VLAN or cloud account.

An appliance is in an offline or sealed state only when:

- Its protected storage data path is physically or cryptographically inaccessible.
- Backup connectors cannot directly address protected storage.
- The cloud control plane cannot mount or browse protected storage.
- Administrative credentials alone cannot expose protected content.
- Decryption keys needed to use the content are unavailable to online services.
- A defined unseal workflow is required before data can be written or restored.

## 2.3 Quantum-safe is the default

Post-quantum protection must be part of the default architecture and not a premium add-on.

NIST finalized ML-KEM, ML-DSA, and SLH-DSA in FIPS 203, FIPS 204, and FIPS 205. Continuity Vault will use standardized post-quantum algorithms and hybrid cryptography to protect data that may need to remain confidential and verifiable for decades.

## 2.4 Cryptographic agility is mandatory

Every encrypted object, signature, key wrap, certificate, and manifest must identify:

- Algorithm
- Parameter set
- Key version
- Cryptographic profile
- Creation date
- Migration state
- Required verification libraries

The architecture must permit algorithms to be changed without rewriting the entire application or losing access to historical backups.

## 2.5 Recovery copies must be independent of source compromise

A compromise of a source account, connector, cloud tenant, or customer administrator must not permit an attacker to:

- Delete historical recovery points
- Rewrite signed manifests
- Reduce retention
- Unwrap recovery keys
- Open an offline appliance
- Approve their own recovery request
- Disable audit collection
- Propagate mass deletions automatically

---

# 3. Deployment Models

# 3.1 Model A: Continuity Vault Public Cloud

Continuity Vault operates the storage, recovery, control, and key-management infrastructure.

## Components

- Continuity Vault SaaS control plane
- Regional ingestion service
- Connector worker environment
- Immutable cloud object storage
- Cross-account recovery storage
- Secondary-region replica
- HSM-backed key-management environment
- Tamper-evident audit ledger
- Restore service
- Customer web and agent interfaces

## Data flow

```text
Source Service
      |
      v
Connector Worker
      |
      v
Encrypted Ingestion Buffer
      |
      v
Validation and Manifest Service
      |
      v
Immutable Primary Recovery Storage
      |
      v
Cross-Account / Cross-Region Replica
```

## Requirements

- Recovery storage must be in a separate cloud account or subscription from connector workers.
- Connector workers must not receive delete privileges on recovery storage.
- Object-lock retention must be enabled.
- Storage-administration and retention-administration roles must be separate.
- At least one replica must use credentials and a failure domain independent of the primary service.
- Customer vaults must have separate key hierarchies.
- Tenant separation must be enforced at the application, identity, storage-prefix, policy, and encryption layers.
- Production operators must not have standing plaintext access.
- Backup data must be encrypted before entering final recovery storage.
- Customer-managed and zero-knowledge key options must be supported.

## Suitable customers

- Consumers
- Families
- Small businesses
- Customers without on-premises infrastructure
- Customers prioritizing ease of deployment

---

# 3.2 Model B: Customer-Owned Public Cloud

The customer supplies a cloud account, subscription, project, or storage tenant. Continuity Vault manages backup policy and operations using delegated access.

## Supported ownership patterns

### Customer storage with Continuity Vault-managed keys

The customer owns the storage account. Continuity Vault operates the key hierarchy.

### Customer storage with customer-managed keys

The customer owns both storage and the root encryption key.

### Customer storage with split-control keys

The customer and Continuity Vault each control part of the authorization required to unwrap data keys.

### Customer storage with zero-knowledge keys

Continuity Vault orchestrates encrypted data but cannot independently decrypt customer content.

## Required cloud constructs

The customer deployment package must provision:

- Dedicated object-storage bucket or container
- Immutable object-lock policy
- Versioning
- Cross-region replication if selected
- Dedicated service identity
- Least-privilege access policy
- Customer key-management integration
- Audit-log export
- Event notification endpoint
- Health-monitoring role
- Cost and capacity alerts

## Access model

Continuity Vault should use:

- Federated workload identity
- Short-lived cloud credentials
- Customer-approved role assumption
- No static access keys
- Separate read, write, restore, and administrative roles
- Customer-visible access logs

## Cloud-loss protection

The architecture must support an optional secondary copy outside the customer’s primary cloud account.

This protects against:

- Subscription deletion
- Cloud administrator compromise
- Billing failure
- Tenant lockout
- Identity-provider failure
- Malicious retention-policy changes

## Suitable customers

- Regulated organizations
- Enterprise customers
- Customers with established cloud commitments
- Customers requiring direct ownership of storage and keys
- Customers with data-residency requirements

---

# 3.3 Model C: Continuity Vault Offline Appliance

Continuity Vault will design and supply a purpose-built hardware appliance that operates as an offline or intermittently connected recovery vault.

The appliance is managed by the Continuity Vault cloud control plane while retaining technical separation between remote management and protected storage.

## Appliance roles

The appliance performs:

- Local encrypted ingestion
- Snapshot validation
- Immutable local retention
- Offline recovery-point storage
- Cryptographic manifest verification
- Deduplication and compression
- Local restore
- Secure cloud-assisted restore orchestration
- Key custody according to the selected model
- Hardware health monitoring
- Signed audit generation

## Appliance deployment locations

- Customer data center
- Customer office
- Family office
- Secure residence
- Colocation facility
- Continuity Vault-operated secure facility
- Managed service-provider facility

---

# 4. Offline Appliance Hardware Architecture

# 4.1 Physical components

A production appliance should contain:

- Redundant server-grade processors
- Error-correcting memory
- Hardware root of trust
- Secure boot
- TPM 2.0 or successor trusted hardware
- Dedicated hardware-security module or secure cryptographic module
- Mirrored boot drives
- Separate protected storage drives
- Hot-spare capacity
- Redundant power supplies
- Chassis-intrusion detection
- Environmental sensors
- Dedicated management network interface
- Dedicated ingestion network interface
- Dedicated restore network interface
- Hardware-controlled storage-path isolation
- Optional cellular out-of-band telemetry
- Tamper-evident enclosure and seals

## Storage options

Product configurations may include:

| Appliance | Usable Capacity | Intended Customer |
|---|---:|---|
| CV Edge 8 | 8 TB | Executive, family office, small business |
| CV Edge 24 | 24 TB | Small and midsized business |
| CV Vault 64 | 64 TB | Midmarket |
| CV Vault 128 | 128 TB | Enterprise |
| CV Cluster | 256 TB+ | Large enterprise and service provider |

Final usable capacity must account for:

- Drive redundancy
- Snapshot history
- Reserved recovery workspace
- Metadata
- Indexes
- Integrity data
- Capacity-overhead thresholds

---

# 4.2 Appliance security zones

The appliance must implement physically and logically distinct zones.

## Zone 1: Management Controller

Responsible for:

- Cloud heartbeat
- Software inventory
- Hardware telemetry
- Remote attestation
- Signed command receipt
- Job scheduling
- Alert transmission

The management controller cannot mount protected storage.

## Zone 2: Ingestion Gateway

Responsible for:

- Receiving encrypted backup packages
- Rate limiting
- Package validation
- Malware scanning where permitted
- Holding packages in temporary staging
- Passing validated packages to the vault transfer process

The ingestion gateway does not possess the customer vault root key.

## Zone 3: Vault Storage Controller

Responsible for:

- Protected storage
- Immutable snapshots
- Manifest verification
- Deduplication
- Retention enforcement
- Local integrity scanning
- Recovery-point inventory

The vault storage controller is inaccessible while sealed.

## Zone 4: Key Security Module

Responsible for:

- Appliance identity keys
- Local key unwrapping
- Recovery authorization enforcement
- Signing appliance attestations
- Protecting split-key shares
- Anti-hammering controls

## Zone 5: Restore Gateway

Responsible for:

- Exporting authorized recovery data
- Applying rate and scope limits
- Re-encrypting data for the destination
- Producing restore evidence
- Ensuring restored data matches its signed manifest

The ingestion and restore gateways must not provide a route through the appliance.

---

# 4.3 Hardware-enforced storage isolation

The preferred implementation uses a hardware-controlled switch between the protected storage controller and network-facing systems.

Possible implementations include:

- PCIe storage-path isolation
- SAS or NVMe fabric switching
- Hardware-controlled power isolation
- Dedicated transfer controller
- Data-diode-assisted replication
- Cryptographic sealing combined with physical bus isolation

Software-defined network controls alone do not satisfy the highest offline classification.

## Appliance states

```text
PROVISIONING
ONLINE_STAGING
READY_TO_SEAL
SEALING
SEALED
UNSEAL_REQUESTED
UNSEALED_FOR_INGEST
UNSEALED_FOR_RECOVERY
VERIFYING
MAINTENANCE
QUARANTINED
DECOMMISSIONING
DESTROYED
```

## State restrictions

| State | Cloud Management | Backup Staging | Protected Storage Accessible | Restore Allowed |
|---|---:|---:|---:|---:|
| Online Staging | Yes | Yes | No | No |
| Sealed | Telemetry only | Optional staging | No | No |
| Unsealed for Ingest | Limited | Yes | Write-controlled | No |
| Unsealed for Recovery | Limited | No | Read-controlled | Yes |
| Maintenance | Restricted | No | Normally no | No |
| Quarantined | Telemetry only | No | No | Emergency process only |

---

# 5. Cloud Management of the Offline Appliance

# 5.1 Outbound-only management

The appliance should initiate management-plane communications to the cloud.

The normal design must not require an inbound internet-accessible management port.

## Management connection

- Appliance establishes an outbound mutually authenticated channel.
- Appliance validates the Continuity Vault control-plane identity.
- Cloud validates the appliance certificate and attestation.
- Communication uses a hybrid post-quantum session-establishment profile.
- Commands are signed, scoped, sequenced, expiring, and replay protected.
- The appliance independently evaluates every command against local policy.
- The cloud cannot override locally enforced retention or key policy through an unsigned administrative instruction.

## Permitted remote management operations

- Retrieve health
- Retrieve storage utilization
- Retrieve signed snapshot status
- Schedule backup windows
- Request an ingest window
- Request a verification job
- Stage a signed software update
- Initiate a restore-approval workflow
- Rotate appliance identity certificates
- Collect non-content diagnostics
- Quarantine the appliance

## Prohibited unilateral cloud operations

The cloud cannot independently:

- Open protected content
- Mount protected storage
- Disable immutability
- Destroy customer keys
- Export full vault content
- Reduce retention
- Add a recovery recipient
- Factory-reset an appliance
- Approve its own privileged command
- Bypass a customer-required local approval

---

# 5.2 Signed command envelope

Every appliance command must use a structure similar to:

```json
{
  "commandId": "uuid",
  "applianceId": "uuid",
  "commandType": "OPEN_INGEST_WINDOW",
  "issuedAt": "RFC3339 timestamp",
  "notBefore": "RFC3339 timestamp",
  "expiresAt": "RFC3339 timestamp",
  "sequence": 18492,
  "requestedBy": "service-or-user-id",
  "approvalSet": [
    {
      "approverId": "uuid",
      "approvalType": "customer-security-admin"
    }
  ],
  "parameters": {
    "maximumDurationSeconds": 1800,
    "expectedSnapshotIds": ["uuid"],
    "maximumBytes": 50000000000
  },
  "policyHash": "sha-384",
  "signatures": {
    "classical": "signature",
    "postQuantum": "signature"
  }
}
```

The appliance must reject commands that are:

- Expired
- Duplicated
- Out of sequence
- Incorrectly signed
- Issued to another appliance
- Inconsistent with local policy
- Missing required approvals
- Broader than the requested operation
- Issued while the appliance is quarantined

---

# 5.3 Remote attestation

Before the control plane trusts appliance status, the appliance must provide signed evidence of:

- Secure-boot state
- Firmware measurements
- Operating-system measurements
- Application version
- Management-controller version
- Vault-controller version
- Security-policy version
- Hardware identity
- Chassis state
- Isolation state
- Last successful integrity scan
- Current storage-controller state

Failed attestation must place the appliance in a restricted or quarantined state.

---

# 6. Backup Workflow for an Offline Appliance

# 6.1 Standard cloud-to-appliance backup

## Step 1: Source collection

A connector retrieves changed data from a source service.

The connector creates:

- Source object record
- Encrypted content package
- Metadata package
- Object checksums
- Connector evidence
- Backup-run identifier

## Step 2: Client or connector encryption

Data is encrypted using a per-object or per-chunk data-encryption key.

The data-encryption key is wrapped under the customer’s collection key.

Where practical, this occurs:

- On the customer endpoint
- Inside the local backup agent
- Inside a customer-trusted connector environment

## Step 3: Cloud staging

The encrypted package enters a temporary ingestion store.

The staging environment may inspect only information permitted by the customer’s encryption mode.

It validates:

- Package structure
- Ciphertext checksum
- Source metadata
- Replay status
- Size and quota
- Connector authorization
- Expected backup-run membership

## Step 4: Appliance notification

The cloud sends the appliance a signed notice containing:

- Snapshot ID
- Package inventory
- Total bytes
- Package hashes
- Retention class
- Expected transfer window

## Step 5: Local approval evaluation

The appliance determines whether:

- The transfer is allowed by local policy.
- Capacity is available.
- Attestation is healthy.
- The expected snapshot is valid.
- The requested ingest window is permitted.

## Step 6: Controlled unseal

The vault storage data path opens only for the transfer controller.

The general management interface remains unable to mount the storage.

## Step 7: Package transfer

Encrypted packages move from staging to the appliance ingestion gateway.

Transfer must support:

- Resume
- Chunk verification
- Bandwidth limits
- Mutual authentication
- Hybrid post-quantum transport security
- Replay resistance
- Package-level integrity

## Step 8: Appliance validation

The appliance verifies:

- Ciphertext hash
- Object count
- Snapshot membership
- Manifest signature
- Backup-run signature
- Retention class
- Key-reference validity
- Duplicate and replay status

## Step 9: Immutable commit

The appliance commits the snapshot into protected storage.

No snapshot becomes a valid recovery point until all required objects and manifest entries have been reconciled.

## Step 10: Appliance seal

The appliance:

- Flushes writes
- Finalizes the immutable snapshot
- Signs a seal receipt
- Closes the storage data path
- Removes temporary transfer credentials
- Returns to the sealed state

## Step 11: Cloud confirmation

The cloud receives a signed receipt containing:

- Appliance ID
- Snapshot ID
- Object count
- Total bytes
- Manifest hash
- Commit timestamp
- Isolation state
- Integrity result
- Signature set

## Step 12: Staging expiration

Cloud staging data is deleted according to policy only after:

- The appliance confirms commit.
- Required cloud replicas confirm commit.
- The snapshot is marked recoverable.
- The minimum staging safety period has elapsed.

---

# 6.2 Direct local backup

A customer may back up local systems directly to the appliance without sending content through Continuity Vault cloud storage.

## Flow

```text
Local Agent
    |
    v
Appliance Ingestion Gateway
    |
    v
Validation Staging
    |
    v
Controlled Vault Transfer
    |
    v
Sealed Protected Storage
```

The cloud receives only job and health metadata unless the customer enables a cloud replica.

This mode supports:

- Local servers
- Endpoints
- NAS devices
- Network exports
- Local password-manager exports
- Local email archives
- Business applications

---

# 6.3 Removable transfer mode

For highly isolated environments, backup packages may be transported using approved removable media.

Required controls:

- Encrypted export package
- Signed manifest
- Media serial-number tracking
- Chain-of-custody log
- Malware scanning
- Import authorization
- One-time transfer token
- Media sanitization after use
- Import receipt
- Duplicate prevention

---

# 7. Restore Workflow from an Offline Appliance

# 7.1 Restore request

The user identifies:

- Recovery point
- Objects
- Destination
- Purpose
- Desired restoration behavior
- Required completion window

## Step 1: Authorization

The platform evaluates:

- User role
- Vault policy
- Collection sensitivity
- Device trust
- Authentication strength
- Required approval quorum
- Legal hold
- Geographic restrictions

## Step 2: Restore plan

The restore service creates a signed plan containing:

- Exact snapshot
- Exact object identifiers
- Expected byte count
- Restore destination
- Conflict behavior
- Maximum session length
- Allowed export format
- Approval evidence

## Step 3: Local approval

For configured deployments, an authorized person must approve the operation physically at the appliance or through a separate customer-controlled authenticator.

## Step 4: Controlled recovery unseal

The appliance enters `UNSEALED_FOR_RECOVERY`.

During this state:

- New backup ingestion is disabled.
- Only the authorized snapshot scope is readable.
- The restore gateway receives a temporary capability.
- General administrative access remains blocked.

## Step 5: Data recovery

The appliance:

1. Verifies the snapshot manifest.
2. Reads selected encrypted objects.
3. Unwraps keys only within the approved key boundary.
4. Decrypts or re-encrypts according to the recovery mode.
5. Streams results through the restore gateway.
6. Verifies exported object checksums.

## Step 6: Destination verification

Where supported, the platform verifies that restored objects exist at the destination and match expected content.

## Step 7: Reseal

After recovery:

- Temporary keys are destroyed.
- Temporary plaintext is erased.
- Restore capabilities expire.
- The storage path closes.
- The appliance returns to `SEALED`.
- A signed recovery report is generated.

---

# 8. Hybrid Cloud and Offline Protection Policies

Customers may define destination policies at the collection level.

## Example policies

### Cloud only

```text
Primary copy: Continuity Vault cloud
Secondary copy: Separate cloud region
Offline copy: None
```

### Appliance only

```text
Primary copy: Customer appliance
Cloud staging: Temporary encrypted staging
Cloud content retention: 24 hours after appliance seal
Cloud metadata: Retained
```

### Cloud plus appliance

```text
Primary copy: Immutable cloud storage
Secondary copy: Offline appliance
Cloud RPO: 1 hour
Appliance RPO: 24 hours
```

### Customer cloud plus appliance

```text
Primary copy: Customer cloud account
Secondary copy: Customer-premises appliance
Control plane: Continuity Vault SaaS
Keys: Customer controlled
```

### Three-copy executive policy

```text
Copy 1: Continuity Vault cloud
Copy 2: Home or office appliance
Copy 3: Geographically separate managed appliance
```

## Policy properties

Each protection policy must define:

- Source
- Destination set
- Backup frequency
- Cloud staging duration
- Cloud retention
- Appliance retention
- Recovery-point objective
- Recovery-time objective
- Immutability period
- Key owner
- Required approvals
- Geographic location
- Verification frequency
- Restore-test frequency
- Deletion behavior
- Succession access

---

# 9. Quantum-Safe Cryptography Standard

# 9.1 Terminology

The product should use the terms:

- **Post-quantum cryptography**
- **Quantum-resistant cryptography**
- **Quantum-safe architecture**

The product must not imply that any cryptographic system provides an absolute guarantee against every future quantum or cryptanalytic development.

Marketing claims should say:

> Continuity Vault uses standardized post-quantum algorithms, hybrid encryption, and crypto-agile key management designed to protect long-lived data against both classical and anticipated quantum-computing threats.

---

# 9.2 Approved baseline algorithms

NIST currently specifies ML-KEM for post-quantum key establishment, ML-DSA as the primary post-quantum digital-signature standard, and SLH-DSA as a signature standard based on a different mathematical approach.

## Data encryption

Use:

- **AES-256-GCM** as the default authenticated content-encryption algorithm.
- An approved misuse-resistant mode where operational requirements justify it.
- Unique nonces as required by the selected mode.
- Independent data-encryption keys by object, chunk group, or snapshot.

AES-256 remains the required minimum symmetric key size because quantum search is generally expected to reduce effective brute-force strength rather than break the underlying construction outright.

## Key establishment and key encapsulation

Default:

- **ML-KEM-768**
- Combined with **X25519** during the hybrid transition period

Higher-assurance profile:

- **ML-KEM-1024**
- Combined with an approved high-strength classical mechanism

Do not use ML-KEM directly as bulk encryption. Use it to establish or encapsulate symmetric key material.

## Digital signatures

Default operational signature profile:

- **ML-DSA-65**
- Combined with **Ed25519** or an approved classical signature during the hybrid transition

Higher-assurance profile:

- **ML-DSA-87**
- Hybrid classical signature
- Optional **SLH-DSA** signature for long-lived root manifests, release attestations, and archival evidence

## Hashing

Use:

- SHA-384 as the default general integrity hash
- SHA-512 where required by a selected profile
- SHA-3 variants where ecosystem support and profile requirements justify them

## Password derivation

Use a memory-hard password-derivation function such as:

- Argon2id

Password-derived keys must not be the sole protection for enterprise or offline-appliance root keys.

---

# 9.3 Hybrid cryptography

Until post-quantum implementations, protocols, libraries, and hardware modules are broadly mature, the platform must combine classical and post-quantum mechanisms.

## Hybrid shared-secret derivation

A session secret should be derived from both:

- A classical exchange secret
- An ML-KEM shared secret

Conceptually:

```text
classical_secret = X25519(...)
pqc_secret = ML-KEM-Decapsulate(...)

session_secret = HKDF(
    classical_secret || pqc_secret,
    context
)
```

The session remains protected as long as at least one properly implemented component remains secure.

## Hybrid signatures

Critical artifacts should contain both classical and post-quantum signatures:

```json
{
  "payloadHash": "sha-384 value",
  "signatures": [
    {
      "algorithm": "Ed25519",
      "keyId": "classical-key-id",
      "signature": "base64"
    },
    {
      "algorithm": "ML-DSA-65",
      "keyId": "pqc-key-id",
      "signature": "base64"
    }
  ]
}
```

Validation policy may require:

- Both signatures valid
- At least one signature valid during an explicitly managed migration
- A policy-selected signature set for historical records

Fail-open behavior is prohibited for current privileged commands.

---

# 9.4 Quantum-safe envelope encryption

Each protected object follows a layered key model.

```text
Customer Root Key
       |
       v
Vault Key
       |
       v
Collection Key
       |
       v
Snapshot Key
       |
       v
Object / Chunk Data-Encryption Key
       |
       v
AES-256-GCM Encrypted Content
```

## Key wrapping

Object data-encryption keys are symmetrically wrapped under the applicable collection or snapshot key.

Long-lived collection and vault keys are protected using:

- HSM-protected symmetric wrapping
- ML-KEM encapsulation to authorized recovery identities
- Hybrid classical and post-quantum wrapping during migration
- Customer key-manager integration where selected

## Multiple recovery recipients

A vault key may be independently wrapped for:

- Primary customer
- Customer recovery device
- Offline appliance HSM
- Enterprise key manager
- Approved successor policy
- Disaster-recovery trustee

A recipient must receive only the wrapped key material authorized for its role.

---

# 9.5 Harvest-now, decrypt-later protection

The platform must assume that attackers may collect encrypted data now and attempt to decrypt it after future cryptographic advances.

Therefore:

- Source-to-cloud transport must use a post-quantum hybrid profile.
- Cloud-to-appliance transport must use a post-quantum hybrid profile.
- Long-lived recovery keys must be wrapped using post-quantum protection.
- Backup manifests must use post-quantum signatures.
- Software-update metadata must use post-quantum signatures.
- Appliance command envelopes must use post-quantum signatures.
- Customer recovery kits must use post-quantum key wrapping.
- Sensitive archived content must not rely solely on RSA or elliptic-curve key protection.

---

# 9.6 Quantum-safe appliance identity

Every appliance receives:

- Classical device-identity key
- ML-DSA device-identity key
- Hardware-bound attestation key
- Recovery-plane identity
- Management-plane identity
- Key-rotation certificate chain

The device-identity private keys must be:

- Generated inside trusted hardware where supported
- Non-exportable
- Protected against cloning
- Rotatable
- Revocable
- Bound to the appliance serial number and deployment record

---

# 9.7 Crypto-agility requirements

The cryptographic subsystem must provide an abstraction layer rather than embedding algorithms throughout business logic.

## Required cryptographic registry

```text
CryptoProfile
Algorithm
ParameterSet
Purpose
Status
IntroducedAt
DeprecatedAt
DisallowedAt
MinimumVerificationDate
MigrationPolicy
LibraryProvider
HardwareSupport
```

## Algorithm states

- Experimental
- Approved
- Preferred
- Legacy verification only
- Deprecated
- Prohibited

## Required migration operations

- Rewrap keys without re-encrypting content
- Add a new recipient
- Remove a recipient
- Add a new signature
- Re-sign a manifest
- Migrate certificate chains
- Re-encrypt selected content
- Verify legacy signatures
- Inventory algorithm usage
- Report quantum-transition readiness

NIST continues to evaluate additional post-quantum algorithms, reinforcing the need for algorithm agility rather than permanent dependence on one construction.

---

# 10. Key Ownership Models

# 10.1 Platform-managed

Continuity Vault manages the root key in a protected HSM environment.

Advantages:

- Simplified recovery
- Low customer operational burden
- Managed rotation

Tradeoff:

- Customer must trust Continuity Vault’s key controls.

# 10.2 Customer-managed

The customer’s cloud key manager or hardware-security module controls the key needed to unwrap the vault key.

Advantages:

- Customer control
- Separation from Continuity Vault
- Easier enterprise compliance alignment

Tradeoff:

- Customer key loss or key-manager lockout may make recovery impossible.

# 10.3 Appliance-held

The appliance HSM holds one required recovery-key share.

Advantages:

- Cloud compromise alone cannot decrypt appliance content.
- Appliance theft alone does not necessarily permit decryption.

# 10.4 Split-control

A recovery authorization requires multiple parties or key shares.

Example:

```text
Required authorization:
- Customer key share
- Appliance HSM share
- Continuity Vault recovery authorization

Threshold:
2 of 3 or 3 of 3, based on policy
```

# 10.5 Zero-knowledge

Continuity Vault never possesses sufficient key material to independently decrypt customer content.

The product must make clear that:

- Support cannot recover a lost zero-knowledge root key.
- Successor access works only when the customer has provisioned an authorized wrapped key or threshold share.
- Search and preview functionality may be more limited.

---

# 11. Appliance Software Update Process

The cloud layer may manage appliance updates, but update installation must not weaken offline isolation.

## Update process

1. Continuity Vault publishes an update manifest.
2. The manifest contains classical and ML-DSA signatures.
3. The appliance downloads the encrypted update into the management zone.
4. The appliance verifies:
   - Publisher signatures
   - Version sequence
   - Hardware compatibility
   - Package hash
   - Rollback policy
   - Security-policy compatibility
5. The update enters a staged state.
6. Customer policy determines whether installation is:
   - Automatic
   - Maintenance-window based
   - Customer approved
   - Local-console approved
7. A rollback partition is retained.
8. Secure boot validates the installed release.
9. Remote attestation confirms the new measurements.
10. Failed attestation triggers rollback or quarantine.

## Update restrictions

- Updates cannot silently reset retention.
- Updates cannot export keys.
- Updates cannot enable new telemetry categories without policy acceptance.
- Downgrades below a security floor are prohibited.
- Emergency updates require auditable emergency authorization.

---

# 12. Appliance Availability During Cloud Outage

The appliance must retain defined local functionality if Continuity Vault’s cloud service is unavailable.

## Local capabilities

Depending on customer policy:

- Continue scheduled local backups
- Validate and seal local snapshots
- Display hardware health
- Perform local integrity checks
- Accept locally authorized emergency recovery
- Queue audit and health events
- Enforce retention
- Operate local identity fallback

## Local emergency recovery

Emergency offline recovery may require:

- Customer-held recovery credential
- Physical presence
- Hardware security key
- Appliance HSM approval
- Recovery quorum
- Time delay
- Locally generated audit package

After connectivity returns, the appliance uploads signed evidence of all offline actions.

---

# 13. Appliance Physical Security and Lifecycle

# 13.1 Shipping and installation

- Unique appliance identity established at manufacturing
- Tamper-evident packaging
- Chain-of-custody tracking
- Customer verifies device fingerprint
- Installation requires activation ceremony
- Device binds to customer vault only after verification
- Default credentials are prohibited

# 13.2 Tamper response

Detected tampering may:

- Quarantine the appliance
- Disable remote commands
- Require local re-attestation
- Seal key operations
- Notify customer security contacts
- Preserve forensic evidence

Automatic destruction of customer keys should not occur solely because a chassis sensor triggers unless the customer has explicitly selected that policy.

# 13.3 Drive failure

Failed drives must remain encrypted.

Replacement workflow:

1. Validate replacement drive.
2. Rebuild redundancy.
3. Verify snapshots.
4. Sanitize or physically destroy failed media.
5. Update media custody record.
6. Produce maintenance evidence.

# 13.4 Decommissioning

1. Customer authorizes decommissioning.
2. Required recovery exports complete.
3. Retention and legal-hold requirements are checked.
4. Key destruction is approved.
5. HSM keys are destroyed.
6. Drives are cryptographically erased.
7. Required physical media destruction occurs.
8. Cloud device identity is revoked.
9. Destruction certificate is issued.
10. Appliance state becomes `DESTROYED`.

---

# 14. Cloud Control-Plane Services

The cloud control plane must include:

- Tenant and vault management
- Identity and passkey service
- Connector orchestration
- Protection-policy engine
- Appliance fleet manager
- Remote-attestation service
- Backup scheduler
- Snapshot catalog
- Recovery-point inventory
- Approval workflow
- Restore orchestration
- Key broker
- Cryptographic-profile registry
- Quantum-transition inventory
- Notification service
- Audit service
- Billing and entitlement service
- Security analytics
- Support administration

## Cloud control-plane data model additions

```text
Appliance
ApplianceModel
ApplianceCertificate
ApplianceAttestation
ApplianceStateEvent
ApplianceCommand
ApplianceCommandApproval
ApplianceStoragePool
ApplianceDrive
ApplianceSnapshotReceipt
ApplianceSoftwareRelease
ApplianceUpdateJob
ApplianceTamperEvent
ApplianceLocation
ApplianceCustodyEvent
CryptoProfile
CryptoKeyReference
CryptoMigrationJob
ProtectionDestination
ReplicationPolicy
TransferWindow
SealReceipt
RecoveryUnsealRequest
```

---

# 15. Required User Experience

# 15.1 Destination selection

During onboarding, the customer selects:

- Continuity Vault cloud
- My cloud account
- My offline appliance
- Cloud and offline appliance
- Help me choose

The UI must explain:

- Who owns the storage
- Who owns the keys
- Whether Continuity Vault can decrypt content
- How often the offline copy is updated
- Where recovery data resides
- What happens during an internet outage
- Expected recovery time
- Additional hardware cost

# 15.2 Appliance dashboard

The customer sees:

- Appliance state
- Last cloud heartbeat
- Last successful attestation
- Protected capacity
- Free capacity
- Last sealed recovery point
- Pending staging volume
- Current RPO status
- Drive health
- Power health
- Temperature
- Software version
- Pending updates
- Tamper state
- Last restore test

## Status terminology

Use specific status descriptions:

- **Online staging available**
- **Transferring encrypted backup**
- **Sealing recovery point**
- **Offline and sealed**
- **Recovery session open**
- **Attestation failed**
- **Hardware maintenance required**

Avoid vague language such as “probably protected.”

---

# 16. Production Acceptance Criteria

The hybrid platform is not production ready until it demonstrates:

## Cloud storage

- Immutable object retention
- Cross-account protection
- Cross-region recovery
- Tenant-isolation testing
- Key-rotation testing
- Full restore test
- Cloud-account-loss test

## Offline appliance

- Hardware storage-path isolation
- Outbound-only normal management
- Signed and replay-protected commands
- Remote attestation
- Controlled unseal and reseal
- Cloud-outage operation
- Drive-failure recovery
- Failed-update rollback
- Tamper workflow
- Full appliance restore
- Decommissioning and key destruction

## Quantum-safe architecture

- ML-KEM-based key encapsulation
- ML-DSA-signed manifests
- Hybrid transport key establishment
- Hybrid signed appliance commands
- Algorithm inventory
- Crypto-profile versioning
- Key rewrapping
- Signature migration
- Legacy verification
- Interoperability testing
- Negative cryptographic tests
- Approved library and implementation review

---

# 17. LLM Build Instructions

An LLM implementing this product must treat the following as non-negotiable:

1. Implement cloud and appliance storage destinations behind a common `ProtectionDestination` interface.
2. Keep control-plane commands separate from backup-content transfer.
3. Never provide the cloud fleet-management service with direct mount access to appliance storage.
4. Model appliance state as a strict server-side and local state machine.
5. Require signed commands for every state transition.
6. Enforce policy locally on the appliance, not only in the cloud.
7. Use ML-KEM for post-quantum key encapsulation.
8. Use ML-DSA for post-quantum signatures.
9. Use AES-256-GCM for content encryption.
10. Use hybrid classical and post-quantum cryptography during the transition period.
11. Place all cryptographic operations behind a versioned provider interface.
12. Never implement cryptographic primitives manually.
13. Preserve algorithm metadata with every encrypted or signed object.
14. Support key rewrapping without mandatory bulk re-encryption.
15. Implement signed snapshot and seal receipts.
16. Test cloud outage, appliance outage, key loss, and connector compromise.
17. Do not classify an appliance as offline unless its protected data path is inaccessible.
18. Do not mark a snapshot recoverable until its destination confirms and signs the commit.
19. Require explicit approval for recovery unseal operations.
20. Produce complete architecture, threat-model, API, state-machine, and operational documentation.

---

# 18. Final System Behavior

The completed system must support the following customer statement:

> My email, files, password-vault backups, identity records, and other critical digital assets are automatically captured and encrypted. I can store them in Continuity Vault’s cloud, my own cloud account, a dedicated offline appliance, or a combination of these locations. The cloud service manages policy and health, but it cannot silently open my offline vault. My long-lived data and recovery records are protected using standardized post-quantum and hybrid cryptography, and every recovery point is independently verified before it is declared usable.

This hybrid deployment architecture is the defining product capability:

- Cloud simplicity where appropriate
- Customer ownership where required
- Physical isolation where necessary
- Central management without continuous content access
- Quantum-safe protection as the default
- Verifiable recovery across all deployment models