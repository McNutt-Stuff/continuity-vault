import { Section } from "../components/Layout";

export default function Terms() {
  const updated = new Date().toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
  return (
    <Section>
      <div className="prose" style={{ margin: "0 auto" }}>
        <div className="eyebrow"><span className="dot" /> Legal</div>
        <h1 style={{ fontSize: 40 }}>Terms of Service</h1>
        <p>Last updated: {updated}</p>
        <p>
          These Terms of Service (the “Terms”) are a binding agreement between you (“you” or “Customer”)
          and Arkive (“Arkive,” “we,” “us,” or “our”) governing your access to and use of the Arkive
          websites, applications, appliances, agents, and related services (together, the “Service”). By
          creating an account, clicking to accept, or using the Service, you agree to these Terms. If you
          do not agree, do not use the Service. If you use the Service on behalf of an organization, you
          represent that you are authorized to bind that organization, and “you” includes that organization.
        </p>

        <h2>1. The Service</h2>
        <p>
          Arkive provides continuity, backup, and recovery tooling that lets you capture copies of data
          from sources you connect and store encrypted copies across cloud storage, bring-your-own storage,
          and optional offline appliances. Arkive is a data-continuity tool — it is not a system of record,
          a legal or regulatory compliance guarantee, an archival service of last resort, or a substitute
          for your own primary systems and independent backups. You are responsible for maintaining your
          original data and accounts.
        </p>

        <h2>2. Accounts, eligibility & security</h2>
        <ul>
          <li>You must be at least the age of majority in your jurisdiction and provide accurate account information.</li>
          <li>
            You are solely responsible for safeguarding your credentials, passkeys, hardware tokens, and
            recovery keys, and for all activity under your account. <b>Because Arkive is private-by-design,
            we cannot recover your data if you lose the keys and recovery material that unlock it.</b> Loss
            of those keys may result in permanent, irreversible loss of access, and Arkive has no obligation
            or ability to restore it.
          </li>
          <li>You will notify us promptly of any unauthorized use of your account.</li>
        </ul>

        <h2>3. Your data & the sources you connect</h2>
        <ul>
          <li>
            As between you and Arkive, you retain all rights to the data you protect (“Customer Data”). You
            grant Arkive a limited license to host, process, transmit, encrypt, and store Customer Data solely
            to provide the Service.
          </li>
          <li>
            You represent and warrant that you own or have all necessary rights, consents, and authority to
            connect each source and to have Arkive back up and process that data, and that doing so does not
            violate any law, contract, or third-party right (including the terms of the source providers you
            connect).
          </li>
          <li>
            You are responsible for the legality of the content you protect and for complying with any laws
            applicable to it (including privacy, data-protection, export, and retention laws).
          </li>
        </ul>

        <h2>4. Third-party services</h2>
        <p>
          The Service connects to third-party providers (for example email, storage, and productivity
          platforms) and may run on third-party cloud infrastructure. Arkive does not control those
          providers and is not responsible for their availability, changes, rate limits, suspensions,
          data loss, security, or acts or omissions. Your use of each third-party service is governed by
          that provider’s own terms. If a provider changes or discontinues access, some features may stop
          working, and that is outside Arkive’s control.
        </p>

        <h2>5. Acceptable use</h2>
        <p>You agree not to, and not to permit anyone to:</p>
        <ul>
          <li>use the Service unlawfully or to store or transmit unlawful, infringing, or malicious content;</li>
          <li>access data or accounts you are not authorized to access;</li>
          <li>interfere with, disrupt, overload, reverse engineer, or attempt to gain unauthorized access to the Service or its infrastructure;</li>
          <li>resell, sublicense, or provide the Service to third parties except as expressly permitted; or</li>
          <li>circumvent security, usage limits, or access controls.</li>
        </ul>
        <p>We may suspend or limit the Service to protect it, other customers, or third parties, or to comply with law.</p>

        <h2>6. Fees, trials & billing</h2>
        <p>
          Paid plans, trials, and pricing are described at sign-up. Unless stated otherwise, fees are billed
          in advance on a recurring basis, are non-refundable except where required by law, and are exclusive
          of taxes, which you are responsible for. Free trials convert to paid plans at the end of the trial
          period unless canceled beforehand. We may change pricing on a prospective basis with notice. You
          authorize us and our payment processors to charge your payment method for all amounts due.
        </p>

        <h2>7. Cancellation, deletion & data portability</h2>
        <p>
          You may cancel at any time. You control retention through your protection policies and may export
          or delete your data. On cancellation or termination, we will delete or make inaccessible your
          Customer Data according to your settings and our routine deletion practices, subject to legal and
          operational retention. It is your responsibility to export any data you wish to keep before
          termination. Immutable and offline copies may persist until they age out under retention.
        </p>

        <h2>8. Service availability</h2>
        <p>
          We work to keep the Service reliable but do not guarantee uninterrupted, error-free, or timely
          operation, or that backups, syncs, or recoveries will always succeed, be complete, or be current.
          The Service may be affected by maintenance, third-party providers, network conditions, or events
          beyond our control. You are responsible for maintaining independent backups of critical data.
        </p>

        <h2>9. Beta & preview features</h2>
        <p>
          Features labeled beta, preview, experimental, or similar are provided “as is,” may change or be
          withdrawn at any time, and are excluded from any service commitments.
        </p>

        <h2>10. Intellectual property</h2>
        <p>
          The Service, including all software, appliances, documentation, and trademarks, is owned by Arkive
          and its licensors and is protected by law. We grant you a limited, non-exclusive, non-transferable,
          revocable right to use the Service during your subscription. All rights not expressly granted are
          reserved. If you provide feedback, you grant us a perpetual, royalty-free license to use it.
        </p>

        <h2>11. Disclaimer of warranties</h2>
        <p>
          TO THE MAXIMUM EXTENT PERMITTED BY LAW, THE SERVICE IS PROVIDED “AS IS” AND “AS AVAILABLE,”
          WITHOUT WARRANTIES OF ANY KIND, WHETHER EXPRESS, IMPLIED, OR STATUTORY, INCLUDING IMPLIED
          WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, TITLE, ACCURACY, AND
          NON-INFRINGEMENT. ARKIVE DOES NOT WARRANT THAT THE SERVICE WILL BE UNINTERRUPTED OR ERROR-FREE,
          THAT DATA WILL NOT BE LOST OR CORRUPTED, OR THAT ANY BACKUP OR RECOVERY WILL BE COMPLETE OR
          SUCCESSFUL. NO ADVICE OR INFORMATION OBTAINED FROM ARKIVE CREATES ANY WARRANTY NOT EXPRESSLY
          STATED HERE.
        </p>

        <h2>12. Limitation of liability</h2>
        <p>
          TO THE MAXIMUM EXTENT PERMITTED BY LAW, ARKIVE AND ITS AFFILIATES, OFFICERS, EMPLOYEES, AND
          SUPPLIERS WILL NOT BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL, EXEMPLARY, OR
          PUNITIVE DAMAGES, OR FOR ANY LOSS OF PROFITS, REVENUE, GOODWILL, OR DATA, OR FOR THE COST OF
          SUBSTITUTE SERVICES, ARISING OUT OF OR RELATED TO THE SERVICE OR THESE TERMS, EVEN IF ADVISED OF
          THE POSSIBILITY OF SUCH DAMAGES. ARKIVE’S TOTAL AGGREGATE LIABILITY FOR ALL CLAIMS ARISING OUT OF
          OR RELATED TO THE SERVICE OR THESE TERMS WILL NOT EXCEED THE GREATER OF (A) THE AMOUNTS YOU PAID
          ARKIVE FOR THE SERVICE IN THE THREE (3) MONTHS IMMEDIATELY BEFORE THE EVENT GIVING RISE TO THE
          CLAIM, OR (B) ONE HUNDRED U.S. DOLLARS (US$100). THESE LIMITATIONS APPLY REGARDLESS OF THE THEORY
          OF LIABILITY AND FORM AN ESSENTIAL BASIS OF THE BARGAIN. SOME JURISDICTIONS DO NOT ALLOW CERTAIN
          EXCLUSIONS, SO SOME OF THE ABOVE MAY NOT APPLY TO YOU.
        </p>

        <h2>13. Indemnification</h2>
        <p>
          You will defend, indemnify, and hold harmless Arkive and its affiliates from and against any
          claims, damages, liabilities, losses, and expenses (including reasonable legal fees) arising out
          of or related to (a) your Customer Data or the sources you connect, (b) your use of the Service,
          (c) your violation of these Terms or applicable law, or (d) your infringement or misappropriation
          of any third-party right.
        </p>

        <h2>14. Term & termination</h2>
        <p>
          These Terms apply while you use the Service. We may suspend or terminate your access at any time
          if you breach these Terms, create risk or legal exposure for us, or as otherwise permitted here.
          Sections that by their nature should survive termination (including ownership, disclaimers,
          limitation of liability, indemnification, and governing law) will survive.
        </p>

        <h2>15. Changes to these Terms</h2>
        <p>
          We may update these Terms from time to time. When we do, we will revise the “Last updated” date
          and, for material changes, provide reasonable notice. Your continued use of the Service after
          changes take effect constitutes acceptance of the updated Terms.
        </p>

        <h2>16. Governing law & disputes</h2>
        <p>
          These Terms are governed by the laws of the United States and the State of Delaware, without regard
          to conflict-of-laws rules. The exclusive venue for any dispute will be the state or federal courts
          located in Delaware, and you consent to their jurisdiction. To the extent permitted by law, any
          claim must be brought within one (1) year after it arises, and you and Arkive each waive any right
          to a jury trial and to participate in a class or representative action.
        </p>

        <h2>17. General</h2>
        <p>
          These Terms, together with our <a href="/privacy">Privacy Policy</a>, are the entire agreement
          between you and Arkive regarding the Service and supersede prior agreements. If any provision is
          held unenforceable, the remaining provisions stay in effect. Our failure to enforce a provision is
          not a waiver. You may not assign these Terms without our consent; we may assign them in connection
          with a merger, acquisition, or sale of assets. Nothing here creates a partnership, agency, or
          employment relationship.
        </p>

        <h2>18. Contact</h2>
        <p>Questions about these Terms? Email <a href="mailto:legal@arkive.life">legal@arkive.life</a>.</p>

        <p style={{ fontSize: 13, marginTop: 24 }}>
          This page is provided for general information and does not constitute legal advice.
        </p>
      </div>
    </Section>
  );
}
