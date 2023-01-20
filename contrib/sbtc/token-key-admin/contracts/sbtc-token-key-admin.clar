
;; title: sbtc-token-key-admin
;; version:
;; summary:
;; description:

;; traits
;;
(impl-trait 'SP3FBR2AGK5H9QBDH3EEN6DF8EK8JY7RX8QJ5SVTE.sip-010-trait-ft-standard.sip-010-trait)

;; token definitions
;; 
(define-fungible-token sbtc u2100000000000000)

;; constants
;;
(define-constant contract-owner tx-sender)
(define-constant err-invalid-caller u1)
(define-constant err-owner-only u100)
(define-constant err-not-token-owner u101)

;; data vars
;;
(define-data-var coordinator (optional {addr: principal, key: (buff 33)}) none)

;; data maps
;;
(define-map signers uint {addr: principal, key: (buff 33)})

;; public functions
;;
(define-public (set-coordinator-data (data {addr: principal, key: (buff 33)}))
    (if (is-valid-caller)
        (ok (var-set coordinator (some data)))
        (err err-invalid-caller)
    )
)

(define-public (set-signer-data (id uint) (data {addr: principal, key: (buff 33)}))
    (if (is-valid-caller)
        (ok (map-set signers id data))
        (err err-invalid-caller)
    )
)

(define-public (delete-signer-data (id uint))
    (if (is-valid-caller)
        (ok (map-delete signers id))
        (err err-invalid-caller)
    )
)

(define-public (mint! (amount uint))
    (if (is-valid-caller)
        (token-credit! tx-sender amount)
        (err err-invalid-caller)
    )
)

(define-public (burn! (amount uint))
    (if (is-valid-caller)
        (token-debit! tx-sender amount)
        (err err-invalid-caller)
    )
)

(define-public (transfer (amount uint) (sender principal) (recipient principal) (memo (optional (buff 34))))
	(begin
		(asserts! (is-eq tx-sender sender) (err err-not-token-owner))
		(try! (ft-transfer? sbtc amount sender recipient))
		(match memo to-print (print to-print) 0x)
		(ok true)
	)
)

;; read only functions
;;
(define-read-only (get-coordinator-data)
    (var-get coordinator)
)

(define-read-only (get-signer-data (signer uint))
    (map-get? signers signer)
)

;;(define-read-only (get-signers)
;;    (map-get? signers)
;;)

(define-read-only (get-name)
	(ok "sBTC")
)

(define-read-only (get-symbol)
	(ok "sBTC")
)

(define-read-only (get-decimals)
	(ok u0)
)

(define-read-only (get-balance (who principal))
	(ok (ft-get-balance sbtc who))
)

(define-read-only (get-total-supply)
	(ok (ft-get-supply sbtc))
)

(define-read-only (get-token-uri)
	(ok (some u"https://github.com/stacks-network/stacks-blockchain"))
)

;; private functions
;;
(define-private (is-valid-caller)
    (is-eq contract-owner tx-sender)
)

(define-private (token-credit! (account principal) (amount uint))
    (ft-mint? sbtc amount account)
)

(define-private (token-debit! (account principal) (amount uint))
    (ft-burn? sbtc amount account)
)