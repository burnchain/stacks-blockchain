use vm::errors::{RuntimeErrorType, InterpreterResult, InterpreterError, 
                 IncomparableError, Error as ClarityError, CheckErrors};
use vm::types::{Value, StandardPrincipalData, OptionalData, PrincipalData, BufferLength,
                TypeSignature, TupleData, QualifiedContractIdentifier, ResponseData};
use vm::database::{ClaritySerializable, ClarityDeserializable};
use vm::representations::{ClarityName, ContractName};

use std::borrow::Borrow;
use std::convert::{TryFrom, TryInto};
use std::collections::HashMap;
use serde_json::{Value as JSONValue};
use util::hash::{hex_bytes, to_hex};

use std::io::{Write, Read};

define_u8_enum!(TypePrefix {
    Int,
    UInt,
    Buffer,
    BoolTrue,
    BoolFalse,
    PrincipalStandard,
    PrincipalContract,
    ResponseOk,
    ResponseErr,
    OptionalNone,
    OptionalSome,
    List,
    Tuple,
});

impl From<&Value> for TypePrefix {
    fn from(v: &Value) -> TypePrefix {
        use super::Value::*;
        use super::PrincipalData::*;

        match v {
            Int(_) => TypePrefix::Int,
            UInt(_) => TypePrefix::UInt,
            Buffer(_) => TypePrefix::Buffer,
            Bool(value) => {
                if *value {
                    TypePrefix::BoolTrue
                } else {
                    TypePrefix::BoolFalse
                }
            },
            Principal(Standard(_)) => TypePrefix::PrincipalStandard,
            Principal(Contract(_)) => TypePrefix::PrincipalContract,
            Response(response) => {
                if response.committed {
                    TypePrefix::ResponseOk
                } else {
                    TypePrefix::ResponseErr
                }
            },
            Optional(OptionalData{ data: None }) => TypePrefix::OptionalNone,
            Optional(OptionalData{ data: Some(value) }) => TypePrefix::OptionalSome,
            List(_) => TypePrefix::List,
            Tuple(_) => TypePrefix::Tuple
        }
    }
}

trait ClarityValueSerializable<T: std::marker::Sized> {
    fn serialize_write<W: Write>(&self, w: &mut W) -> std::io::Result<()>;
    fn deserialize_read<R: Read>(r: &mut R) -> Result<T, SerializationError>;
}

impl ClarityValueSerializable<StandardPrincipalData> for StandardPrincipalData {
    fn serialize_write<W: Write>(&self, w: &mut W) -> std::io::Result<()> {
        w.write_all(&[self.0])?;
        w.write_all(&self.1)
    }

    fn deserialize_read<R: Read>(r: &mut R) -> Result<Self, SerializationError> {
        let mut version = [0; 1];
        let mut data = [0; 20];
        r.read_exact(&mut version)?;
        r.read_exact(&mut data)?;
        Ok(StandardPrincipalData(version[0], data))
    }
}

macro_rules! serialize_guarded_string {
    ($Name:ident) => {

impl ClarityValueSerializable<$Name> for $Name {
    fn serialize_write<W: Write>(&self, w: &mut W) -> std::io::Result<()> {
        w.write_all(&u32::try_from(self.as_str().len())
                    .unwrap()
                    .to_be_bytes())?;
        // self.as_bytes() is always len bytes, because this is only used for GuardedStrings
        //   which are a subset of ASCII
        w.write_all(self.as_str().as_bytes())
    }

    fn deserialize_read<R: Read>(r: &mut R) -> Result<Self, SerializationError> {
        let mut len = [0; 4];
        r.read_exact(&mut len)?;
        let len = u32::from_be_bytes(len);
        let mut data = vec![0; len as usize];
        r.read_exact(&mut data)?;

        String::from_utf8(data)
            .map_err(|_| "Non-UTF8 string data".into())
            .and_then(|x| $Name::try_from(x)
                      .map_err(|_| "Illegal Clarity string".into()))
    }
}

}}

serialize_guarded_string!(ClarityName);
serialize_guarded_string!(ContractName);

#[derive(Debug, PartialEq)]
pub enum SerializationError {
    IoError(IncomparableError<std::io::Error>),
    BadTypeError(CheckErrors),
    DeserializationError(String),
    DeserializeExpected(TypeSignature),
}

impl From<std::io::Error> for SerializationError {
    fn from(err: std::io::Error) -> Self {
        match err.kind() {
            std::io::ErrorKind::UnexpectedEof => "Unexpected end of byte stream".into(),
            _ => SerializationError::IoError(IncomparableError { err })
        }
    }
}

impl From<&str> for SerializationError {
    fn from(e: &str) -> Self {
        SerializationError::DeserializationError(e.into())
    }
}

impl From<CheckErrors> for SerializationError {
    fn from(e: CheckErrors) -> Self {
        SerializationError::BadTypeError(e)
    }
}

macro_rules! check_match {
    ($item:expr, $Pattern:pat) => {
        match $item {
            None => Ok(()),
            Some($Pattern) => Ok(()),
            Some(x) => Err(SerializationError::DeserializeExpected(x.clone()))
        }
    }
}

impl Value {
    fn deserialize_read<R: Read>(r: &mut R, expected_type: Option<&TypeSignature>) -> Result<Value, SerializationError> {
        use super::Value::*;
        use super::PrincipalData::*;

        let mut header = [0];
        r.read_exact(&mut header)?;

        let prefix = TypePrefix::from_u8(header[0])
            .ok_or_else(|| "Bad type prefix")?;

        match prefix {
            TypePrefix::Int => {
                check_match!(expected_type, TypeSignature::IntType)?;
                let mut buffer = [0; 16];
                r.read_exact(&mut buffer)?;
                Ok(Int(i128::from_be_bytes(buffer)))
            },
            TypePrefix::UInt => {
                check_match!(expected_type, TypeSignature::UIntType)?;
                let mut buffer = [0; 16];
                r.read_exact(&mut buffer)?;
                Ok(UInt(u128::from_be_bytes(buffer)))
            },
            TypePrefix::Buffer => {
                let mut buffer_len = [0; 4];
                r.read_exact(&mut buffer_len)?;
                let buffer_len = BufferLength::try_from(
                    u32::from_be_bytes(buffer_len))?;

                if let Some(x) = expected_type {
                    let passed_test = match x {
                        TypeSignature::BufferType(expected_len) => {
                            u32::from(&buffer_len) <= u32::from(expected_len)
                        },
                        _ => false
                    };
                    if !passed_test {
                        return Err(SerializationError::DeserializeExpected(x.clone()))
                    }
                }

                let mut data = vec![0; u32::from(buffer_len) as usize];

                r.read_exact(&mut data[..])?;

                // can safely unwrap, because the buffer length was _already_ checked.
                Ok(Value::buff_from(data).unwrap())
            },
            TypePrefix::BoolTrue => {
                check_match!(expected_type, TypeSignature::BoolType)?;
                Ok(Bool(true))
            },
            TypePrefix::BoolFalse => {
                check_match!(expected_type, TypeSignature::BoolType)?;
                Ok(Bool(false))
            },
            TypePrefix::PrincipalStandard => {
                check_match!(expected_type, TypeSignature::PrincipalType)?;
                StandardPrincipalData::deserialize_read(r)
                    .map(Value::from)
            },
            TypePrefix::PrincipalContract => {
                check_match!(expected_type, TypeSignature::PrincipalType)?;
                let issuer = StandardPrincipalData::deserialize_read(r)?;
                let name = ContractName::deserialize_read(r)?;
                Ok(Value::from(QualifiedContractIdentifier { issuer, name }))
            },
            TypePrefix::ResponseOk | TypePrefix::ResponseErr => {
                let committed = prefix == TypePrefix::ResponseOk;

                let expect_contained_type = match expected_type {
                    None => None,
                    Some(x) => {
                        let contained_type = match (committed, x) {
                            (true, TypeSignature::ResponseType(types)) => Ok(&types.0),
                            (false, TypeSignature::ResponseType(types)) => Ok(&types.1),
                            _ => Err(SerializationError::DeserializeExpected(x.clone()))
                        }?;
                        Some(contained_type)
                    }
                };

                let data = Box::new(Value::deserialize_read(r, expect_contained_type)?);
                Ok(Response(ResponseData { committed, data }))
            },
            TypePrefix::OptionalNone => {
                check_match!(expected_type, TypeSignature::OptionalType(_))?;
                Ok(Value::none())
            },
            TypePrefix::OptionalSome => {
                let expect_contained_type = match expected_type {
                    None => None,
                    Some(x) => {
                        let contained_type = match x {
                            TypeSignature::OptionalType(some_type) => Ok(some_type.as_ref()),
                            _ => Err(SerializationError::DeserializeExpected(x.clone()))
                        }?;
                        Some(contained_type)
                    }
                };

                Ok(Value::some(Value::deserialize_read(r, expect_contained_type)?))
            }
            TypePrefix::List => {
                let mut len = [0; 4];
                r.read_exact(&mut len)?;
                let len = u32::from_be_bytes(len);

                let (list_type, entry_type) = match expected_type {
                    None => (None, None),
                    Some(TypeSignature::ListType(list_type)) => {
                        if len > list_type.get_max_len() {
                            return Err(SerializationError::DeserializeExpected(
                                expected_type.unwrap().clone()))
                        }
                        (Some(list_type), Some(list_type.get_list_item_type()))
                    },
                    Some(x) => return Err(SerializationError::DeserializeExpected(x.clone()))
                };

                let mut items = Vec::with_capacity(len as usize);
                for _i in 0..len {
                    items.push(Value::deserialize_read(r, entry_type)?);
                }

                if let Some(list_type) = list_type {
                    Value::list_with_type(items, list_type.clone())
                        .map_err(|_| "Illegal list type".into())
                } else {
                    Value::list_from(items)
                        .map_err(|_| "Illegal list type".into())
                }
            },
            TypePrefix::Tuple => {
                let mut len = [0; 4];
                r.read_exact(&mut len)?;
                let len = u32::from_be_bytes(len);

                let tuple_type = match expected_type {
                    None => None,
                    Some(TypeSignature::TupleType(tuple_type)) => {
                        if len as u64 != tuple_type.len() {
                            return Err(SerializationError::DeserializeExpected(
                                expected_type.unwrap().clone()))
                        }
                        Some(tuple_type)
                    },
                    Some(x) => return Err(SerializationError::DeserializeExpected(x.clone()))
                };

                let mut items = Vec::with_capacity(len as usize);
                for _i in 0..len {
                    let key = ClarityName::deserialize_read(r)?;

                    let expected_field_type = match tuple_type {
                        None => None,
                        Some(some_tuple) => Some(
                            some_tuple
                                .field_type(&key)
                                .ok_or_else(|| SerializationError::DeserializeExpected(expected_type.unwrap().clone()))?)
                    };

                    let value = Value::deserialize_read(r, expected_field_type)?;
                    items.push((key, value))
                }

                if let Some(tuple_type) = tuple_type {
                    TupleData::from_data_typed(items, tuple_type)
                        .map_err(|_| "Illegal tuple type".into())
                        .map(Value::from)
                } else {
                    TupleData::from_data(items)
                        .map_err(|_| "Illegal tuple type".into())
                        .map(Value::from)
                }
            }
        }

    }

    fn serialize_write<W: Write>(&self, w: &mut W) -> std::io::Result<()> {
        use super::Value::*;
        use super::PrincipalData::*;

        w.write_all(&[TypePrefix::from(self) as u8])?;
        match self {
            Int(value) => w.write_all(&value.to_be_bytes())?,
            UInt(value) => w.write_all(&value.to_be_bytes())?,
            Buffer(value) => {
                w.write_all(&(u32::from(value.len()).to_be_bytes()))?;
                w.write_all(&value.data)?
            }
            Principal(Standard(data)) => {
                data.serialize_write(w)?
            },
            Principal(Contract(contract_identifier)) => {
                contract_identifier.issuer.serialize_write(w)?;
                contract_identifier.name.serialize_write(w)?;
            },
            Response(response) => {
                response.data.serialize_write(w)?
            },
            // Bool types don't need any more data.
            Bool(_) => {},
            // None types don't need any more data.
            Optional(OptionalData{ data: None }) => {},
            Optional(OptionalData{ data: Some(value) }) => {
                value.serialize_write(w)?;
            },
            List(data) => {
                w.write_all(&data.len().to_be_bytes())?;
                for item in data.data.iter() {
                    item.serialize_write(w)?;
                }
            },
            Tuple(data) => {
                w.write_all(&u32::try_from(data.data_map.len())
                            .unwrap()
                            .to_be_bytes())?;
                for (key, value) in data.data_map.iter() {
                    key.serialize_write(w)?;
                    value.serialize_write(w)?;
                }
            }
        };

        Ok(())
    }
}

impl ClaritySerializable for Value {
    fn serialize(&self) -> String {
        let mut byte_serialization = Vec::new();
        self.serialize_write(&mut byte_serialization)
            .expect("IOError filling byte buffer.");
        to_hex(byte_serialization.as_slice())
    }
}

impl Value {
    /// This function attempts to deserialize a JSONParser struct into a Clarity Value.
    ///   The `expected_type` parameter determines whether or not the deserializer should expect (and enforce)
    ///   a particular type. `ClarityDB` uses this to ensure that lists, tuples, etc. loaded from the database
    ///   have their max-length and other type information set by the type declarations in the contract.
    ///   If passed `None`, the deserializer will construct the values as if they were literals in the contract, e.g.,
    ///     list max length = the length of the list.

    pub fn try_deserialize_hex(hex: &str, expected: &TypeSignature) -> Result<Value, SerializationError> {
        let data = hex_bytes(hex)
            .map_err(|_| "Bad hex string")?;
        Value::deserialize_read(&mut data.as_slice(), Some(expected))
            .map_err(|e| match e {
                SerializationError::IoError(e) => panic!("Should not have received IO Error: {:?}", e),
                _ => e
            })
    }

    pub fn try_deserialize_hex_untyped(hex: &str) -> Result<Value, SerializationError> {
        let data = hex_bytes(hex)
            .map_err(|_| "Bad hex string")?;
        Value::deserialize_read(&mut data.as_slice(), None)
            .map_err(|e| match e {
                SerializationError::IoError(e) => panic!("Should not have received IO Error: {:?}", e),
                _ => e
            })
    }
}

impl Value {
    pub fn deserialize(json: &str, expected: &TypeSignature) -> Self {
        Value::try_deserialize_hex(json, expected)
            .expect("ERROR: Failed to parse Clarity hex string")
    }
}


#[cfg(test)]
mod tests {
    use super::SerializationError;
    use vm::database::ClaritySerializable;
    use vm::errors::Error;
    use super::super::*;
    use vm::types::TypeSignature::{IntType, BoolType};

    fn buff_type(size: u32) -> TypeSignature {
        TypeSignature::BufferType(size.try_into().unwrap()).into()
    }


    fn test_deser_ser(v: Value) {
        assert_eq!(&v, &Value::deserialize(&v.serialize(), &TypeSignature::type_of(&v)));
        assert_eq!(&v, &Value::try_deserialize_hex_untyped(&v.serialize())
                   .unwrap());
    }

    fn test_bad_expectation(v: Value, e: TypeSignature) {
        assert!(
            match Value::try_deserialize_hex(&v.serialize(), &e).unwrap_err() {
                SerializationError::DeserializeExpected(_) => true,
                _ => false
            })
    }

    #[test]
    fn test_lists() {
       
        let list_list_int = Value::list_from(vec![
            Value::list_from(vec![Value::Int(1), Value::Int(2), Value::Int(3)]).unwrap()
        ]).unwrap();

        // Should be legal!
        Value::try_deserialize_hex(
            &Value::list_from(vec![]).unwrap().serialize(),
            &TypeSignature::from("(list 2 (list 3 int))")).unwrap();
        Value::try_deserialize_hex(
            &list_list_int.serialize(),
            &TypeSignature::from("(list 2 (list 3 int))")).unwrap();
        Value::try_deserialize_hex(
            &list_list_int.serialize(),
            &TypeSignature::from("(list 1 (list 4 int))")).unwrap();

        test_deser_ser(list_list_int.clone());
        test_deser_ser(Value::list_from(vec![]).unwrap());
        test_bad_expectation(list_list_int.clone(), TypeSignature::BoolType);
        // inner type isn't expected
        test_bad_expectation(list_list_int.clone(), TypeSignature::from("(list 1 (list 4 uint))"));
        // child list longer than expected
        test_bad_expectation(list_list_int.clone(), TypeSignature::from("(list 1 (list 2 uint))"));
        // parent list longer than expected
        test_bad_expectation(list_list_int.clone(), TypeSignature::from("(list 0 (list 2 uint))"));
    }

    #[test]
    fn test_bools() {
        test_deser_ser(Value::Bool(false));
        test_deser_ser(Value::Bool(true));

        test_bad_expectation(Value::Bool(false), TypeSignature::IntType);
        test_bad_expectation(Value::Bool(true), TypeSignature::IntType);
    }

    #[test]
    fn test_ints() {
        test_deser_ser(Value::Int(0));
        test_deser_ser(Value::Int(1));
        test_deser_ser(Value::Int(-1));
        test_deser_ser(Value::Int(i128::max_value()));
        test_deser_ser(Value::Int(i128::min_value()));

        test_bad_expectation(Value::Int(1), TypeSignature::UIntType);
    }

    #[test]
    fn test_uints() {
        test_deser_ser(Value::UInt(0));
        test_deser_ser(Value::UInt(1));
        test_deser_ser(Value::UInt(u128::max_value()));
        test_deser_ser(Value::UInt(u128::min_value()));

        test_bad_expectation(Value::UInt(1), TypeSignature::IntType);
    }

    #[test]
    fn test_opts() {
        test_deser_ser(Value::none());
        test_deser_ser(Value::some(Value::Int(15)));

        test_bad_expectation(Value::none(), TypeSignature::IntType);
        test_bad_expectation(Value::some(Value::Int(15)), TypeSignature::IntType);
        // bad expected _contained_ type
        test_bad_expectation(Value::some(Value::Int(15)), TypeSignature::from("(optional uint)"));
    }

    #[test]
    fn test_resp() {
        test_deser_ser(Value::okay(Value::Int(15)));
        test_deser_ser(Value::error(Value::Int(15)));

        // Bad expected types.
        test_bad_expectation(Value::okay(Value::Int(15)), TypeSignature::IntType);
        test_bad_expectation(Value::okay(Value::Int(15)), TypeSignature::from("(response uint int)"));
        test_bad_expectation(Value::error(Value::Int(15)), TypeSignature::from("(response int uint)"));
    }

    #[test]
    fn test_buffs() {
        test_deser_ser(Value::buff_from(vec![0,0,0,0]).unwrap());
        test_deser_ser(Value::buff_from(vec![0xde,0xad,0xbe,0xef]).unwrap());
        test_deser_ser(Value::buff_from(vec![0,0xde,0xad,0xbe,0xef,0]).unwrap());

        test_bad_expectation(
            Value::buff_from(vec![0,0xde,0xad,0xbe,0xef,0]).unwrap(),
            TypeSignature::BoolType);

        // fail because we expect a shorter buffer
        test_bad_expectation(
            Value::buff_from(vec![0,0xde,0xad,0xbe,0xef,0]).unwrap(),
            TypeSignature::from("(buff 2)"));
        
    }

    #[test]
    fn test_tuples() {
        let t_1 = Value::from(TupleData::from_data(vec![
            ("a".into(), Value::Int(1)),
            ("b".into(), Value::Int(1))]).unwrap());
        let t_0 = Value::from(TupleData::from_data(vec![
            ("b".into(), Value::Int(1)),
            ("a".into(), Value::Int(1))]).unwrap());
        let t_2 = Value::from(TupleData::from_data(vec![
            ("a".into(), Value::Int(1)),
            ("b".into(), Value::Bool(true))]).unwrap());
        let t_3 = Value::from(TupleData::from_data(vec![
            ("a".into(), Value::Int(1))]).unwrap());
        let t_4 = Value::from(TupleData::from_data(vec![
            ("a".into(), Value::Int(1)),
            ("c".into(), Value::Bool(true))]).unwrap());

        test_deser_ser(t_0.clone());
        test_deser_ser(t_1.clone());
        test_deser_ser(t_2.clone());
        test_deser_ser(t_3.clone());

        test_bad_expectation(t_0.clone(), TypeSignature::BoolType);

        // t_0 and t_1 are actually the same
        assert_eq!(
            Value::try_deserialize_hex(&t_1.serialize(), &TypeSignature::type_of(&t_0)).unwrap(),
            Value::try_deserialize_hex(&t_0.serialize(), &TypeSignature::type_of(&t_0)).unwrap());

        // field number not equal to expectations
        assert!(match Value::try_deserialize_hex(
            &t_3.serialize(),
            &TypeSignature::type_of(&t_1)).unwrap_err() {
            SerializationError::DeserializeExpected(_) => true,
             _ => false
        });

        // field type mismatch
        assert!(match Value::try_deserialize_hex(
            &t_2.serialize(),
            &TypeSignature::type_of(&t_1)).unwrap_err() {
            SerializationError::DeserializeExpected(_) => true,
             _ => false
        });

        // field not-present in expected
        assert!(match Value::try_deserialize_hex(
            &t_1.serialize(),
            &TypeSignature::type_of(&t_4)).unwrap_err() {
            SerializationError::DeserializeExpected(_) => true,
             _ => false
        });
    }

    #[test]
    fn test_principals() {
        let issuer = PrincipalData::parse_standard_principal("SM2J6ZY48GV1EZ5V2V5RB9MP66SW86PYKKQVX8X0G").unwrap();
        let standard_p = Value::from(issuer.clone());

        let contract_identifier = QualifiedContractIdentifier::new(issuer, "foo".into());
        let contract_p2 = Value::from(PrincipalData::Contract(contract_identifier));

        test_deser_ser(contract_p2.clone());
        test_deser_ser(standard_p.clone());

        test_bad_expectation(contract_p2, TypeSignature::BoolType);
        test_bad_expectation(standard_p, TypeSignature::BoolType);
    }

}
